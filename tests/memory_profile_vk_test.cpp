// Exercise the actual Vulkan memory wrappers with driver spies
#include "../gpu/vk/vk_internal.h"
#include <cassert>
#include <cstdio>
#include <vector>

namespace mkwvk { Device dev; const MkwVkHost* host; }
struct Event { int kind; uint64_t id, size; };
static std::vector<Event> events;
static VkResult allocation_result = VK_SUCCESS;
static int allocations, frees;
static int free_boundaries;
void memory_profile_boundary_spy() { free_boundaries++; }

static VkResult VKAPI_CALL allocate_spy(VkDevice, const VkMemoryAllocateInfo*, const VkAllocationCallbacks*, VkDeviceMemory* memory) {
    allocations++;
    *memory = (VkDeviceMemory)(uintptr_t)1234;
    return allocation_result;
}
static void VKAPI_CALL free_spy(VkDevice, VkDeviceMemory, const VkAllocationCallbacks*) {
    // Removal precedes handle reuse by another allocation thread
    assert(!events.empty() && events.back().size == 0);
    assert(free_boundaries == frees + 1);
    frees++;
}
namespace vkx {
PFN_vkAllocateMemory vkAllocateMemory = allocate_spy;
PFN_vkFreeMemory vkFreeMemory = free_spy;
}
static void record(int kind, uint64_t id, uint64_t, uint64_t size) { events.push_back({kind, id, size}); }

int main() {
    using namespace mkwvk;
    MkwVkHost callbacks{};
    callbacks.memory_event = record;
    host = &callbacks;
    dev.memory.memoryTypes[0].propertyFlags = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT;
    dev.memory.memoryTypes[1].propertyFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    VkMemoryAllocateInfo info{};
    info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
    info.allocationSize = 8192;
    VkDeviceMemory memory{};
    assert(profile_allocate_memory({}, &info, nullptr, &memory) == VK_SUCCESS);
    assert(events.back().kind == HOS_MEM_VK_LOCAL && events.back().size == 8192);
    profile_free_memory({}, memory, nullptr);
    info.memoryTypeIndex = 1;
    profile_allocate_memory({}, &info, nullptr, &memory);
    assert(events.back().kind == HOS_MEM_VK_HOST);
    VkImportMemoryHostPointerInfoEXT imported{};
    imported.sType = VK_STRUCTURE_TYPE_IMPORT_MEMORY_HOST_POINTER_INFO_EXT;
    info.pNext = &imported;
    profile_allocate_memory({}, &info, nullptr, &memory);
    assert(events.back().kind == HOS_MEM_VK_IMPORTED);
    allocation_result = VK_ERROR_OUT_OF_DEVICE_MEMORY;
    profile_allocate_memory({}, &info, nullptr, &memory);
    assert(events.back().kind == HOS_MEM_FAILURE);
    size_t count = events.size();
    callbacks.memory_event = nullptr;
    profile_allocate_memory({}, &info, nullptr, &memory);
    assert(events.size() == count && allocations == 5 && frees == 1);
    std::puts("PASS: local/host allocations, imported aliases, failures and disabled profiling");
}
