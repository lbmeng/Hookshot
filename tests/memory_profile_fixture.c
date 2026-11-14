/* Synthetic allocations exercise telemetry without game files or a GPU */
#define _POSIX_C_SOURCE 200809L
#include "../gpu/vk/memory_profile.h"
#include "../gpu/vk/vk_api.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#include <windows.h>
static int64_t now_ns(void) {
    LARGE_INTEGER t, f; QueryPerformanceCounter(&t); QueryPerformanceFrequency(&f);
    return (t.QuadPart / f.QuadPart) * 1000000000LL + (t.QuadPart % f.QuadPart) * 1000000000LL / f.QuadPart;
}
static void pause_ms(unsigned ms) { Sleep(ms); }
static DWORD WINAPI allocations(void *arg) {
#else
#include <pthread.h>
#include <time.h>
static int64_t now_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t); return t.tv_sec * 1000000000LL + t.tv_nsec; }
static void pause_ms(unsigned ms) { struct timespec t = {ms / 1000, (ms % 1000) * 1000000L}; nanosleep(&t, NULL); }
static void *allocations(void *arg) {
#endif
    const uint64_t id = 100 + (uintptr_t)arg;
    for (int i = 0; i < 200; i++) {
        hos_memory_event(HOS_MEM_VK_BUFFER, id, 0, 8192);
        hos_memory_event(HOS_MEM_VK_BUFFER, id, 0, 0);
    }
    return 0;
}

int main(int argc, char **argv) {
    hos_memory_profile_start(now_ns);
    const char *mb = getenv("HOS_MEMORY_MB");
    uint64_t budget = mb ? strtoull(mb, NULL, 10) : 9809;
    hos_memory_profile_budget(budget << 20);
    hos_memory_event(HOS_MEM_HEAP, 0, 0x1000, 0x4000);
    hos_memory_event(HOS_MEM_REGION, 1, 0x3000, 0x4000);
    hos_memory_event(HOS_MEM_NVN_POOL, 10, 0x2000, 0x2000);
    hos_memory_event(HOS_MEM_NVN_POOL, 11, 0x3000, 0x2000);
    hos_memory_event(HOS_MEM_PAGES, 0x10000, 0x10000, 0x4000);
    hos_memory_unmap(0x11000, 0x2000);
    hos_memory_event(HOS_MEM_PAGES, 0x11000, 0x11000, 0x2000);
    hos_memory_unmap(0x10000, 0x4000);
    hos_memory_event(HOS_MEM_VK_LOCAL, 7, 0, 48u << 20);
    hos_memory_event(HOS_MEM_VK_LOCAL, 7, 0, 0);
    hos_memory_event(HOS_MEM_FAILURE, HOS_MEM_VK_LOCAL, 0, 4096);
    hos_memory_event(HOS_MEM_RENDERER, MKWVK_API_VERSION, 0, 0);
#ifdef _WIN32
    HANDLE threads[4];
    for (uintptr_t i = 0; i < 4; i++) threads[i] = CreateThread(NULL, 0, allocations, (void *)i, 0, NULL);
    for (int i = 0; i < 4; i++) { WaitForSingleObject(threads[i], INFINITE); CloseHandle(threads[i]); }
#else
    pthread_t threads[4];
    for (uintptr_t i = 0; i < 4; i++) pthread_create(&threads[i], NULL, allocations, (void *)i);
    for (int i = 0; i < 4; i++) pthread_join(threads[i], NULL);
#endif
    for (int i = 0; i + 1 < argc; i++) {
        if (!strcmp(argv[i], "--user")) {
            char path[4096]; snprintf(path, sizeof path, "%s/fixture-output.txt", argv[i + 1]);
            FILE *f = fopen(path, "w"); if (f) { fputs("synthetic fixture\n", f); fclose(f); }
        }
    }
    for (int i = 0; i < 30; i++) { hos_memory_event(HOS_MEM_PRESENT, 0, 0, 0); pause_ms(10); }
    hos_memory_profile_stop();
    const char *threshold = getenv("FIXTURE_EXIT_BELOW");
    puts("PASS: synthetic memory telemetry fixture, not a game measurement");
    return threshold && budget < strtoull(threshold, NULL, 10) ? 3 : 0;
}
