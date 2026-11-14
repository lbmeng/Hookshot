/* Allocation peaks are recorded on mutations, including allocations freed between samples */
#define _POSIX_C_SOURCE 200809L
#include "../gpu/vk/memory_profile.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
static SRWLOCK mutex = SRWLOCK_INIT;
#define LOCK() AcquireSRWLockExclusive(&mutex)
#define UNLOCK() ReleaseSRWLockExclusive(&mutex)
static HANDLE worker;
#else
#include <pthread.h>
#include <time.h>
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
#define LOCK() pthread_mutex_lock(&mutex)
#define UNLOCK() pthread_mutex_unlock(&mutex)
static pthread_t worker;
#endif

typedef struct { int kind; uint64_t id, address, size; } Entry;
typedef struct { uint64_t lo, hi; } Span;
static Entry *entries;
static size_t count, capacity;
static Span *spans;
static size_t span_capacity;
static uint64_t current[HOS_MEM_COUNT], interval_peak[HOS_MEM_COUNT];
static uint64_t guest_union, pool_union, guest_peak, pool_peak, failures, telemetry_errors, budget, sequence, presents;
static uint64_t explicit_union, explicit_peak;
static int stopping, started, interval_ms = 100;
static int renderer_version;
static uint64_t failure_kind, failure_request;
static FILE *output;
static int64_t (*clock_ns)(void);
static const char *phase_path;
static const char *names[] = {"guest_image", "guest_system", "guest_heap", "guest_regions", "guest_pages",
    "nvn_pool_registered", "vk_local_blocks", "vk_host_blocks", "vk_imported", "vk_image_allocations", "vk_buffer_allocations",
    "vk_render_target_allocations", "vk_host_buffer_allocations"};
int hos_memory_profile_enabled;

static int span_compare(const void *a, const void *b) {
    const Span *x = a, *y = b;
    return x->lo < y->lo ? -1 : x->lo > y->lo;
}

static uint64_t union_size(int mode) {
    size_t n = 0;
    if (span_capacity < count) {
        Span *p = realloc(spans, count * sizeof *p);
        if (!p) { telemetry_errors++; return UINT64_MAX; }
        spans = p;
        span_capacity = count;
    }
    for (size_t i = 0; i < count; i++) {
        const Entry *e = &entries[i];
        if (mode == 1 ? e->kind != HOS_MEM_NVN_POOL :
            e->kind > HOS_MEM_PAGES || (mode == 2 && e->kind == HOS_MEM_REGION)) continue;
        spans[n++] = (Span){e->address, e->address + e->size};
    }
    if (!n) return 0;
    qsort(spans, n, sizeof *spans, span_compare);
    uint64_t total = 0, lo = spans[0].lo, hi = spans[0].hi;
    for (size_t i = 1; i < n; i++) {
        if (spans[i].lo > hi) { total += hi - lo; lo = spans[i].lo; }
        if (spans[i].hi > hi) hi = spans[i].hi;
    }
    return total + hi - lo;
}

static void update_peaks(int kind) {
    if (kind <= HOS_MEM_PAGES) {
        guest_union = union_size(0);
        if (guest_union > guest_peak) guest_peak = guest_union;
        explicit_union = union_size(2);
        if (explicit_union > explicit_peak) explicit_peak = explicit_union;
    }
    if (kind == HOS_MEM_NVN_POOL) {
        pool_union = union_size(1);
        if (pool_union > pool_peak) pool_peak = pool_union;
    }
    if (current[kind] > interval_peak[kind]) interval_peak[kind] = current[kind];
}

void hos_memory_event(int kind, uint64_t id, uint64_t address, uint64_t size) {
    if (!hos_memory_profile_enabled) return;
    LOCK();
    if (kind == HOS_MEM_FAILURE) {
        failures++; failure_kind = id; failure_request = size;
        fprintf(stderr, "memory profile: allocation failure kind=%llu requested=%llu address=%#llx\n",
            (unsigned long long)id, (unsigned long long)size, (unsigned long long)address);
        UNLOCK(); return;
    }
    if (kind == HOS_MEM_PRESENT) { presents++; UNLOCK(); return; }
    if (kind == HOS_MEM_RENDERER) { renderer_version = (int)id; UNLOCK(); return; }
    if (kind < 0 || kind >= HOS_MEM_COUNT || size > UINT64_MAX - address) {
        telemetry_errors++; UNLOCK(); return;
    }
    if (kind == HOS_MEM_PAGES && size) {
        uint64_t lo = address, hi = address + size;
        for (size_t j = 0; j < count;) {
            Entry *e = &entries[j];
            if (e->kind != kind || e->address > hi || e->address + e->size < lo) { j++; continue; }
            if (e->address < lo) lo = e->address;
            if (e->address + e->size > hi) hi = e->address + e->size;
            current[kind] -= e->size;
            entries[j] = entries[--count];
            j = 0;
        }
        id = address = lo; size = hi - lo;
    }
    size_t i;
    for (i = 0; i < count; i++) if (entries[i].kind == kind && entries[i].id == id) break;
    if (i < count) current[kind] -= entries[i].size;
    else if (size) {
        if (count == capacity) {
            size_t cap = capacity ? capacity * 2 : 256;
            Entry *p = realloc(entries, cap * sizeof *p);
            if (!p) { telemetry_errors++; UNLOCK(); return; }
            entries = p; capacity = cap;
        }
        i = count++;
    }
    if (size) {
        entries[i] = (Entry){kind, id, address, size};
        current[kind] += size;
    } else if (i < count) entries[i] = entries[--count];
    update_peaks(kind);
    UNLOCK();
}

/* Physical-page unmaps may split an earlier map, rather than remove it whole */
void hos_memory_unmap(uint64_t address, uint64_t size) {
    if (!hos_memory_profile_enabled || !size) return;
    LOCK();
    if (size > UINT64_MAX - address) { telemetry_errors++; UNLOCK(); return; }
    const uint64_t end = address + size;
    for (size_t i = 0; i < count;) {
        Entry *e = &entries[i];
        if (e->kind != HOS_MEM_PAGES || e->address >= end || e->address + e->size <= address) { i++; continue; }
        const uint64_t lo = e->address, hi = lo + e->size;
        if (lo < address && hi > end) {
            if (count == capacity) {
                Entry *p = realloc(entries, (capacity * 2) * sizeof *p);
                if (!p) { telemetry_errors++; break; }
                entries = p; capacity *= 2; e = &entries[i];
            }
            entries[count++] = (Entry){HOS_MEM_PAGES, end, end, hi - end};
            e->size = address - lo;
            current[HOS_MEM_PAGES] -= size;
        } else if (lo < address) { e->size = address - lo; current[HOS_MEM_PAGES] -= hi - address; }
        else if (hi > end) { e->id = e->address = end; e->size = hi - end; current[HOS_MEM_PAGES] -= end - lo; }
        else { current[HOS_MEM_PAGES] -= e->size; entries[i] = entries[--count]; continue; }
        i++;
    }
    update_peaks(HOS_MEM_PAGES);
    UNLOCK();
}

void hos_memory_profile_budget(uint64_t bytes) {
    if (!hos_memory_profile_enabled) return;
    LOCK(); budget = bytes; UNLOCK();
}

static void sample(void) {
    char phase[96] = "unmarked";
    if (phase_path) {
        FILE *f = fopen(phase_path, "r");
        if (f) {
            if (fgets(phase, sizeof phase, f)) {
                for (size_t i = 0; phase[i]; i++)
                    if (!((phase[i] >= 'a' && phase[i] <= 'z') || (phase[i] >= 'A' && phase[i] <= 'Z') ||
                          (phase[i] >= '0' && phase[i] <= '9') || phase[i] == '-' || phase[i] == '_' || phase[i] == '.')) {
                        phase[i] = 0; break;
                    }
            }
            fclose(f);
        }
    }
    LOCK();
    fprintf(output, "%llu,%lld,%s,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%llu,%d,%llu,%llu", (unsigned long long)sequence++,
        (long long)clock_ns(), phase, (unsigned long long)budget, (unsigned long long)guest_union,
        (unsigned long long)guest_peak, (unsigned long long)pool_union, (unsigned long long)pool_peak,
        (unsigned long long)failures, (unsigned long long)telemetry_errors, (unsigned long long)presents, renderer_version,
        (unsigned long long)failure_kind, (unsigned long long)failure_request);
    for (int k = 0; k < HOS_MEM_COUNT; k++) {
        fprintf(output, ",%llu,%llu", (unsigned long long)current[k], (unsigned long long)interval_peak[k]);
        interval_peak[k] = current[k];
    }
    fprintf(output, ",%llu,%llu", (unsigned long long)explicit_union, (unsigned long long)explicit_peak);
    fputc('\n', output);
    if (fflush(output)) telemetry_errors++;
    guest_peak = guest_union; pool_peak = pool_union;
    explicit_peak = explicit_union;
    UNLOCK();
}

#ifdef _WIN32
static DWORD WINAPI sampler(void *unused) {
#else
static void *sampler(void *unused) {
#endif
    (void)unused;
    for (;;) {
        LOCK(); int done = stopping; UNLOCK();
        if (done) break;
        sample();
#ifdef _WIN32
        Sleep((DWORD)interval_ms);
#else
        struct timespec delay = {interval_ms / 1000, (interval_ms % 1000) * 1000000L};
        nanosleep(&delay, NULL);
#endif
    }
    return 0;
}

void hos_memory_profile_stop(void) {
    if (!started) return;
    LOCK(); stopping = 1; UNLOCK();
#ifdef _WIN32
    WaitForSingleObject(worker, INFINITE); CloseHandle(worker);
#else
    pthread_join(worker, NULL);
#endif
    sample();
    fclose(output);
    started = 0;
    /* Allocation threads may still be exiting, so the ledger lives until process exit */
}

void hos_memory_profile_start(int64_t (*now_ns)(void)) {
    const char *path = getenv("HOS_MEMORY_PROFILE");
    if (!path || !*path || started) return;
    output = fopen(path, "wx");
    if (!output) { fprintf(stderr, "memory profile: cannot create %s\n", path); return; }
    clock_ns = now_ns;
    clock_ns();
    const char *phase = getenv("HOS_MEMORY_PHASE_FILE");
    if (phase) {
        char *copy = malloc(strlen(phase) + 1);
        if (copy) { strcpy(copy, phase); phase_path = copy; }
        else telemetry_errors++;
    }
    const char *ms = getenv("HOS_MEMORY_SAMPLE_MS");
    if (ms) { interval_ms = atoi(ms); if (interval_ms < 10) interval_ms = 10; if (interval_ms > 1000) interval_ms = 1000; }
    fputs("sequence,monotonic_ns,phase,budget_bytes,guest_address_union_bytes,guest_address_union_interval_peak_bytes,"
          "nvn_pool_unique_address_bytes,nvn_pool_unique_address_interval_peak_bytes,allocation_failures,telemetry_errors,presents,renderer_api,"
          "last_failure_kind,last_failure_request_bytes", output);
    for (int k = 0; k < HOS_MEM_COUNT; k++) fprintf(output, ",%s_bytes,%s_interval_peak_bytes", names[k], names[k]);
    fputs(",guest_explicit_union_bytes,guest_explicit_union_interval_peak_bytes", output);
    fputc('\n', output); fflush(output);
    hos_memory_profile_enabled = 1;
#ifdef _WIN32
    worker = CreateThread(NULL, 0, sampler, NULL, 0, NULL);
    started = worker != NULL;
#else
    started = pthread_create(&worker, NULL, sampler, NULL) == 0;
#endif
    if (!started) { hos_memory_profile_enabled = 0; fclose(output); fprintf(stderr, "memory profile: cannot start sampler\n"); return; }
    atexit(hos_memory_profile_stop);
    fprintf(stderr, "memory profile: enabled, interval %d ms\n", interval_ms);
}
