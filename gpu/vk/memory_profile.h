/* Optional allocation telemetry shared by the runtime and Vulkan backend */
#pragma once
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

enum HosMemoryKind {
    HOS_MEM_IMAGE,
    HOS_MEM_SYSTEM,
    HOS_MEM_HEAP,
    HOS_MEM_REGION,
    HOS_MEM_PAGES,
    HOS_MEM_NVN_POOL,
    HOS_MEM_VK_LOCAL,
    HOS_MEM_VK_HOST,
    HOS_MEM_VK_IMPORTED,
    HOS_MEM_VK_IMAGE,
    HOS_MEM_VK_BUFFER,
    HOS_MEM_VK_RENDER_TARGET,
    HOS_MEM_VK_HOST_BUFFER,
    HOS_MEM_COUNT,
    HOS_MEM_FAILURE,
    HOS_MEM_PRESENT,
    HOS_MEM_RENDERER
};

/* A zero size removes (kind, id); address is used only for guest range unions */
void hos_memory_event(int kind, uint64_t id, uint64_t address, uint64_t size);
void hos_memory_unmap(uint64_t address, uint64_t size);
void hos_memory_profile_start(int64_t (*now_ns)(void));
void hos_memory_profile_stop(void);
void hos_memory_profile_budget(uint64_t bytes);
extern int hos_memory_profile_enabled;

#ifdef __cplusplus
}
#endif
