// Device-memory blocks and imported aliases have separate ledgers
#include "vk_internal.h"
#include "vk_work.h"

namespace mkwvk {

void profile_memory_block(uint32_t type, VkDeviceMemory memory, uint64_t size) {
    const bool local = (dev.memory.memoryTypes[type].propertyFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) != 0;
    memory_profile_event(local ? HOS_MEM_VK_LOCAL : HOS_MEM_VK_HOST, (uint64_t)memory, 0, size);
}

VkResult profile_allocate_memory(VkDevice device, const VkMemoryAllocateInfo* info,
                                const VkAllocationCallbacks* callbacks, VkDeviceMemory* memory) {
    const VkResult result = vkAllocateMemory(device, info, callbacks, memory);
    if (!host || !host->memory_event) return result;
    if (result != VK_SUCCESS) {
        memory_profile_event(HOS_MEM_FAILURE, HOS_MEM_VK_LOCAL, 0, info->allocationSize);
        return result;
    }
    bool imported = false;
    for (auto p = (const VkBaseInStructure*)info->pNext; p; p = p->pNext)
        if (p->sType == VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT ||
            p->sType == VK_STRUCTURE_TYPE_IMPORT_MEMORY_FD_INFO_KHR ||
            p->sType == VK_STRUCTURE_TYPE_IMPORT_MEMORY_WIN32_HANDLE_INFO_KHR) imported = true;
    if (imported) memory_profile_event(HOS_MEM_VK_IMPORTED, (uint64_t)*memory, 0, info->allocationSize);
    else profile_memory_block(info->memoryTypeIndex, *memory, info->allocationSize);
    return result;
}

void profile_free_memory(VkDevice device, VkDeviceMemory memory, const VkAllocationCallbacks* callbacks) {
    if (memory) {
        memory_profile_event(HOS_MEM_VK_LOCAL, (uint64_t)memory, 0, 0);
        memory_profile_event(HOS_MEM_VK_HOST, (uint64_t)memory, 0, 0);
        memory_profile_event(HOS_MEM_VK_IMPORTED, (uint64_t)memory, 0, 0);
    }
    vkFreeMemory(device, memory, callbacks);
}

} // namespace mkwvk
