/* VK_LAYER_BOL_inputlat — find the first presented frame whose picture moved.
 *
 * At every vkQueuePresentKHR on a large swapchain, a copy of a grid of single
 * pixels is chained in front of the present on the same queue (waiting on the
 * application's present semaphores, signalling a semaphore the present then
 * waits on). A reader thread waits for that copy, compares the grid with the
 * previous frame of the same swapchain and logs one line per frame:
 *
 *   F <seq> <presentId> <t_qp_ns> <t_qp_ret_ns> <t_gpu_ns> <changed> <points> <w> <h>
 *
 * vkWaitForPresentKHR / vkWaitForPresent2KHR are timed too:
 *
 *   W <presentId> <t_call_ns> <t_ret_ns> <result>
 *
 * vkAcquireNextImageKHR:
 *
 *   A <t_call_ns> <t_ret_ns> <image> <result>
 *
 * All times are CLOCK_MONOTONIC, the clock the injector logs with.
 * Log path: $INPUTLAT_LOG (default /tmp/inputlat-<pid>.log), one file per pid.
 */
#define _GNU_SOURCE
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define EXPORT __attribute__((visibility("default")))
#define GX 24
#define GY 14
#define NPTS (GX * GY)
#define NSLOTS 32
#define MAXIMG 16

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static pthread_mutex_t g_lock = PTHREAD_MUTEX_INITIALIZER;
static pthread_mutex_t g_log_lock = PTHREAD_MUTEX_INITIALIZER;
static FILE *g_log;

static void logf_(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
#include <stdarg.h>
static void logf_(const char *fmt, ...)
{
    va_list ap;
    pthread_mutex_lock(&g_log_lock);
    if (!g_log)
    {
        const char *p = getenv("INPUTLAT_LOG");
        char buf[512];
        if (p && *p)
            snprintf(buf, sizeof(buf), "%s.%d", p, (int)getpid());
        else
            snprintf(buf, sizeof(buf), "/tmp/inputlat-%d.log", (int)getpid());
        g_log = fopen(buf, "a");
    }
    if (g_log)
    {
        va_start(ap, fmt);
        vfprintf(g_log, fmt, ap);
        va_end(ap);
        fflush(g_log);
    }
    pthread_mutex_unlock(&g_log_lock);
}

static void *dkey(const void *h) { return *(void *const *)h; }

/* ---------------------------------------------------------------- instance */
struct inst
{
    void *key;
    VkInstance instance;
    PFN_vkGetInstanceProcAddr gipa;
    PFN_vkDestroyInstance DestroyInstance;
    PFN_vkGetPhysicalDeviceMemoryProperties GetPhysicalDeviceMemoryProperties;
};
static struct inst g_inst[8];
static int g_ninst;

static struct inst *inst_find(void *key)
{
    for (int i = 0; i < g_ninst; i++)
        if (g_inst[i].key == key)
            return &g_inst[i];
    return g_ninst ? &g_inst[g_ninst - 1] : NULL;
}

/* ------------------------------------------------------------------ device */
struct slot
{
    VkCommandBuffer cmd;
    VkFence fence;
    VkSemaphore sem;
    int state; /* 0 free, 1 pending */
    uint64_t seq, present_id, t_qp, t_qp_ret;
    struct swch *sc;
};

struct dev
{
    void *key;
    VkDevice device;
    VkPhysicalDevice phys;
    struct inst *inst;
    PFN_vkGetDeviceProcAddr gdpa;
    PFN_vkSetDeviceLoaderData set_loader_data;

    PFN_vkDestroyDevice DestroyDevice;
    PFN_vkGetDeviceQueue GetDeviceQueue;
    PFN_vkGetDeviceQueue2 GetDeviceQueue2;
    PFN_vkCreateSwapchainKHR CreateSwapchainKHR;
    PFN_vkDestroySwapchainKHR DestroySwapchainKHR;
    PFN_vkGetSwapchainImagesKHR GetSwapchainImagesKHR;
    PFN_vkQueuePresentKHR QueuePresentKHR;
    PFN_vkAcquireNextImageKHR AcquireNextImageKHR;
    PFN_vkWaitForPresentKHR WaitForPresentKHR;
    VkResult (VKAPI_PTR *WaitForPresent2KHR)(VkDevice, VkSwapchainKHR, const void *);
    PFN_vkCreateCommandPool CreateCommandPool;
    PFN_vkAllocateCommandBuffers AllocateCommandBuffers;
    PFN_vkBeginCommandBuffer BeginCommandBuffer;
    PFN_vkEndCommandBuffer EndCommandBuffer;
    PFN_vkResetCommandBuffer ResetCommandBuffer;
    PFN_vkCmdPipelineBarrier CmdPipelineBarrier;
    PFN_vkCmdCopyImageToBuffer CmdCopyImageToBuffer;
    PFN_vkQueueSubmit QueueSubmit;
    PFN_vkCreateFence CreateFence;
    PFN_vkWaitForFences WaitForFences;
    PFN_vkResetFences ResetFences;
    PFN_vkCreateSemaphore CreateSemaphore;
    PFN_vkCreateBuffer CreateBuffer;
    PFN_vkGetBufferMemoryRequirements GetBufferMemoryRequirements;
    PFN_vkAllocateMemory AllocateMemory;
    PFN_vkBindBufferMemory BindBufferMemory;
    PFN_vkMapMemory MapMemory;
    PFN_vkInvalidateMappedMemoryRanges InvalidateMappedMemoryRanges;
    PFN_vkDeviceWaitIdle DeviceWaitIdle;

    VkQueue queues[64];
    uint32_t queue_family[64];
    int nqueues;

    /* readback, created lazily for one queue family */
    int rb_ready;
    uint32_t rb_family;
    VkCommandPool pool;
    VkBuffer buf;
    VkDeviceMemory mem;
    VkDeviceSize mem_size;
    int mem_coherent;
    uint8_t *map;
    struct slot slots[NSLOTS];
    uint64_t next_seq;
    unsigned head; /* next slot to fill */
    unsigned tail; /* next slot the reader processes */
    pthread_t reader;
    int reader_started;
    pthread_mutex_t rb_lock;
    pthread_cond_t rb_cond;
};
static struct dev g_dev[8];
static int g_ndev;

static struct dev *dev_find(void *key)
{
    for (int i = 0; i < g_ndev; i++)
        if (g_dev[i].key == key)
            return &g_dev[i];
    return NULL;
}

/* --------------------------------------------------------------- swapchain */
struct swch
{
    VkSwapchainKHR handle;
    struct dev *dev;
    uint32_t w, h;
    VkFormat format;
    int readable;
    uint32_t nimages;
    VkImage images[MAXIMG];
    uint8_t prev[NPTS * 4];
    int have_prev;
    int live;
};
static struct swch g_sc[64];

static struct swch *sc_find(VkSwapchainKHR h)
{
    for (int i = 0; i < 64; i++)
        if (g_sc[i].live && g_sc[i].handle == h)
            return &g_sc[i];
    return NULL;
}

/* ------------------------------------------------------------------ reader */
static void *reader_main(void *arg)
{
    struct dev *d = arg;
    for (;;)
    {
        pthread_mutex_lock(&d->rb_lock);
        while (d->tail == d->head)
            pthread_cond_wait(&d->rb_cond, &d->rb_lock);
        struct slot *s = &d->slots[d->tail % NSLOTS];
        pthread_mutex_unlock(&d->rb_lock);

        VkResult r = d->WaitForFences(d->device, 1, &s->fence, VK_TRUE, 2000000000ull);
        uint64_t t_gpu = now_ns();
        struct swch *sc = s->sc;
        int changed = -1;
        if (r == VK_SUCCESS)
        {
            uint8_t *px = d->map + (size_t)(d->tail % NSLOTS) * NPTS * 4;
            if (!d->mem_coherent)
            {
                VkMappedMemoryRange range = {VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE, NULL, d->mem, 0, VK_WHOLE_SIZE};
                d->InvalidateMappedMemoryRanges(d->device, 1, &range);
            }
            if (sc && sc->live)
            {
                changed = 0;
                if (sc->have_prev)
                {
                    for (int i = 0; i < NPTS; i++)
                    {
                        int diff = abs(px[i * 4] - sc->prev[i * 4]) + abs(px[i * 4 + 1] - sc->prev[i * 4 + 1])
                                + abs(px[i * 4 + 2] - sc->prev[i * 4 + 2]);
                        if (diff > 24)
                            changed++;
                    }
                }
                else
                    changed = NPTS;
                memcpy(sc->prev, px, NPTS * 4);
                sc->have_prev = 1;
            }
            d->ResetFences(d->device, 1, &s->fence);
        }
        logf_("F %llu %llu %llu %llu %llu %d %d %u %u\n", (unsigned long long)s->seq,
                (unsigned long long)s->present_id, (unsigned long long)s->t_qp,
                (unsigned long long)s->t_qp_ret, (unsigned long long)t_gpu, changed, NPTS,
                sc ? sc->w : 0, sc ? sc->h : 0);
        if (r != VK_SUCCESS)
        {
            /* Leave the slot pending; a lost copy would desynchronise everything. */
            logf_("E fence wait %d\n", r);
            continue;
        }
        pthread_mutex_lock(&d->rb_lock);
        s->state = 0;
        d->tail++;
        pthread_cond_broadcast(&d->rb_cond);
        pthread_mutex_unlock(&d->rb_lock);
    }
    return NULL;
}

static int rb_init(struct dev *d, uint32_t family)
{
    VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL,
            VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, family};
    if (d->CreateCommandPool(d->device, &pci, NULL, &d->pool) != VK_SUCCESS)
        return 0;
    VkCommandBufferAllocateInfo ai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, d->pool,
            VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
    for (int i = 0; i < NSLOTS; i++)
    {
        if (d->AllocateCommandBuffers(d->device, &ai, &d->slots[i].cmd) != VK_SUCCESS)
            return 0;
        if (d->set_loader_data)
            d->set_loader_data(d->device, d->slots[i].cmd);
        else
            *(void **)d->slots[i].cmd = *(void **)d->device;
        VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, NULL, 0};
        VkSemaphoreCreateInfo sci = {VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO, NULL, 0};
        if (d->CreateFence(d->device, &fci, NULL, &d->slots[i].fence) != VK_SUCCESS)
            return 0;
        if (d->CreateSemaphore(d->device, &sci, NULL, &d->slots[i].sem) != VK_SUCCESS)
            return 0;
    }
    VkBufferCreateInfo bci = {VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bci.size = (VkDeviceSize)NSLOTS * NPTS * 4;
    bci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (d->CreateBuffer(d->device, &bci, NULL, &d->buf) != VK_SUCCESS)
        return 0;
    VkMemoryRequirements req;
    d->GetBufferMemoryRequirements(d->device, d->buf, &req);
    VkPhysicalDeviceMemoryProperties mp;
    d->inst->GetPhysicalDeviceMemoryProperties(d->phys, &mp);
    int best = -1, best_score = -1;
    for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
    {
        VkMemoryPropertyFlags f = mp.memoryTypes[i].propertyFlags;
        if (!(req.memoryTypeBits & (1u << i)) || !(f & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT))
            continue;
        int score = ((f & VK_MEMORY_PROPERTY_HOST_CACHED_BIT) ? 2 : 0) + ((f & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT) ? 1 : 0);
        if (score > best_score)
            best = (int)i, best_score = score;
    }
    if (best < 0)
        return 0;
    d->mem_coherent = !!(mp.memoryTypes[best].propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    VkMemoryAllocateInfo mai = {VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, NULL, req.size, (uint32_t)best};
    if (d->AllocateMemory(d->device, &mai, NULL, &d->mem) != VK_SUCCESS)
        return 0;
    d->BindBufferMemory(d->device, d->buf, d->mem, 0);
    if (d->MapMemory(d->device, d->mem, 0, VK_WHOLE_SIZE, 0, (void **)&d->map) != VK_SUCCESS)
        return 0;
    d->rb_family = family;
    pthread_mutex_init(&d->rb_lock, NULL);
    pthread_cond_init(&d->rb_cond, NULL);
    if (pthread_create(&d->reader, NULL, reader_main, d) == 0)
        d->reader_started = 1;
    logf_("I readback ready family=%u memtype=%d coherent=%d\n", family, best, d->mem_coherent);
    return d->reader_started;
}

static uint32_t queue_family(struct dev *d, VkQueue q)
{
    for (int i = 0; i < d->nqueues; i++)
        if (d->queues[i] == q)
            return d->queue_family[i];
    return UINT32_MAX;
}

static void remember_queue(struct dev *d, VkQueue q, uint32_t family)
{
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < d->nqueues; i++)
        if (d->queues[i] == q)
        {
            pthread_mutex_unlock(&g_lock);
            return;
        }
    if (d->nqueues < 64)
    {
        d->queues[d->nqueues] = q;
        d->queue_family[d->nqueues] = family;
        d->nqueues++;
    }
    pthread_mutex_unlock(&g_lock);
}

/* ----------------------------------------------------------------- entries */
static VKAPI_ATTR VkResult VKAPI_CALL l_CreateInstance(const VkInstanceCreateInfo *ci,
        const VkAllocationCallbacks *alloc, VkInstance *out)
{
    VkLayerInstanceCreateInfo *lci = (VkLayerInstanceCreateInfo *)ci->pNext;
    while (lci && !(lci->sType == VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO && lci->function == VK_LAYER_LINK_INFO))
        lci = (VkLayerInstanceCreateInfo *)lci->pNext;
    if (!lci)
        return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = lci->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    lci->u.pLayerInfo = lci->u.pLayerInfo->pNext;
    PFN_vkCreateInstance create = (PFN_vkCreateInstance)gipa(VK_NULL_HANDLE, "vkCreateInstance");
    VkResult r = create(ci, alloc, out);
    if (r != VK_SUCCESS)
        return r;
    pthread_mutex_lock(&g_lock);
    if (g_ninst < 8)
    {
        struct inst *in = &g_inst[g_ninst++];
        in->key = dkey(*out);
        in->instance = *out;
        in->gipa = gipa;
        in->DestroyInstance = (PFN_vkDestroyInstance)gipa(*out, "vkDestroyInstance");
        in->GetPhysicalDeviceMemoryProperties =
                (PFN_vkGetPhysicalDeviceMemoryProperties)gipa(*out, "vkGetPhysicalDeviceMemoryProperties");
    }
    pthread_mutex_unlock(&g_lock);
    logf_("I instance created\n");
    return r;
}

static VKAPI_ATTR void VKAPI_CALL l_DestroyInstance(VkInstance instance, const VkAllocationCallbacks *alloc)
{
    struct inst *in = inst_find(dkey(instance));
    if (in)
        in->DestroyInstance(instance, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL l_CreateDevice(VkPhysicalDevice phys, const VkDeviceCreateInfo *ci,
        const VkAllocationCallbacks *alloc, VkDevice *out)
{
    VkLayerDeviceCreateInfo *lci = (VkLayerDeviceCreateInfo *)ci->pNext;
    PFN_vkSetDeviceLoaderData sld = NULL;
    for (VkLayerDeviceCreateInfo *p = (VkLayerDeviceCreateInfo *)ci->pNext; p; p = (VkLayerDeviceCreateInfo *)p->pNext)
        if (p->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && p->function == VK_LOADER_DATA_CALLBACK)
            sld = p->u.pfnSetDeviceLoaderData;
    while (lci && !(lci->sType == VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO && lci->function == VK_LAYER_LINK_INFO))
        lci = (VkLayerDeviceCreateInfo *)lci->pNext;
    if (!lci)
        return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = lci->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = lci->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    lci->u.pLayerInfo = lci->u.pLayerInfo->pNext;
    struct inst *in = inst_find(dkey(phys));
    PFN_vkCreateDevice create = (PFN_vkCreateDevice)gipa(in ? in->instance : VK_NULL_HANDLE, "vkCreateDevice");
    VkResult r = create(phys, ci, alloc, out);
    if (r != VK_SUCCESS)
        return r;
    pthread_mutex_lock(&g_lock);
    if (g_ndev >= 8)
    {
        pthread_mutex_unlock(&g_lock);
        return r;
    }
    struct dev *d = &g_dev[g_ndev++];
    memset(d, 0, sizeof(*d));
    d->key = dkey(*out);
    d->device = *out;
    d->phys = phys;
    d->inst = in;
    d->gdpa = gdpa;
    d->set_loader_data = sld;
#define G(n) d->n = (void *)gdpa(*out, "vk" #n)
    G(DestroyDevice); G(GetDeviceQueue); G(GetDeviceQueue2); G(CreateSwapchainKHR); G(DestroySwapchainKHR);
    G(GetSwapchainImagesKHR); G(QueuePresentKHR); G(AcquireNextImageKHR); G(WaitForPresentKHR); G(WaitForPresent2KHR);
    G(CreateCommandPool); G(AllocateCommandBuffers); G(BeginCommandBuffer); G(EndCommandBuffer);
    G(ResetCommandBuffer); G(CmdPipelineBarrier); G(CmdCopyImageToBuffer); G(QueueSubmit); G(CreateFence);
    G(WaitForFences); G(ResetFences); G(CreateSemaphore); G(CreateBuffer); G(GetBufferMemoryRequirements);
    G(AllocateMemory); G(BindBufferMemory); G(MapMemory); G(InvalidateMappedMemoryRanges); G(DeviceWaitIdle);
#undef G
    pthread_mutex_unlock(&g_lock);
    logf_("I device created present_wait=%d present_wait2=%d\n", d->WaitForPresentKHR != NULL,
            d->WaitForPresent2KHR != NULL);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL l_DestroyDevice(VkDevice device, const VkAllocationCallbacks *alloc)
{
    struct dev *d = dev_find(dkey(device));
    if (!d)
        return;
    /* The reader thread is left alone; the process is about to tear down. */
    d->DestroyDevice(device, alloc);
}

static VKAPI_ATTR void VKAPI_CALL l_GetDeviceQueue(VkDevice device, uint32_t family, uint32_t index, VkQueue *q)
{
    struct dev *d = dev_find(dkey(device));
    d->GetDeviceQueue(device, family, index, q);
    if (*q)
        remember_queue(d, *q, family);
}

static VKAPI_ATTR void VKAPI_CALL l_GetDeviceQueue2(VkDevice device, const VkDeviceQueueInfo2 *info, VkQueue *q)
{
    struct dev *d = dev_find(dkey(device));
    d->GetDeviceQueue2(device, info, q);
    if (*q)
        remember_queue(d, *q, info->queueFamilyIndex);
}

static VKAPI_ATTR VkResult VKAPI_CALL l_CreateSwapchainKHR(VkDevice device, const VkSwapchainCreateInfoKHR *ci,
        const VkAllocationCallbacks *alloc, VkSwapchainKHR *out)
{
    struct dev *d = dev_find(dkey(device));
    VkSwapchainCreateInfoKHR mod = *ci;
    mod.imageUsage |= VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    int readable = 1;
    VkResult r = d->CreateSwapchainKHR(device, &mod, alloc, out);
    if (r != VK_SUCCESS)
    {
        readable = 0;
        r = d->CreateSwapchainKHR(device, ci, alloc, out);
    }
    logf_("S %llu %ux%u fmt=%d mode=%d minImages=%u usage=0x%x readable=%d r=%d t=%llu\n",
            (unsigned long long)(uintptr_t)*out, ci->imageExtent.width, ci->imageExtent.height, ci->imageFormat,
            ci->presentMode, ci->minImageCount, ci->imageUsage, readable, r, (unsigned long long)now_ns());
    if (r != VK_SUCCESS)
        return r;
    pthread_mutex_lock(&g_lock);
    for (int i = 0; i < 64; i++)
    {
        if (g_sc[i].live)
            continue;
        struct swch *sc = &g_sc[i];
        memset(sc, 0, sizeof(*sc));
        sc->handle = *out;
        sc->dev = d;
        sc->w = ci->imageExtent.width;
        sc->h = ci->imageExtent.height;
        sc->format = ci->imageFormat;
        sc->readable = readable && sc->w >= 320 && sc->h >= 200;
        uint32_t n = 0;
        d->GetSwapchainImagesKHR(device, *out, &n, NULL);
        if (n > MAXIMG)
            n = MAXIMG;
        d->GetSwapchainImagesKHR(device, *out, &n, sc->images);
        sc->nimages = n;
        sc->live = 1;
        break;
    }
    pthread_mutex_unlock(&g_lock);
    return r;
}

static VKAPI_ATTR void VKAPI_CALL l_DestroySwapchainKHR(VkDevice device, VkSwapchainKHR sc,
        const VkAllocationCallbacks *alloc)
{
    struct dev *d = dev_find(dkey(device));
    struct swch *s = sc_find(sc);
    if (s && s->readable && d->reader_started)
    {
        /* Pending copies still read this swapchain's images. */
        pthread_mutex_lock(&d->rb_lock);
        while (d->tail != d->head)
            pthread_cond_wait(&d->rb_cond, &d->rb_lock);
        pthread_mutex_unlock(&d->rb_lock);
    }
    if (s)
        s->live = 0;
    d->DestroySwapchainKHR(device, sc, alloc);
}

static VKAPI_ATTR VkResult VKAPI_CALL l_AcquireNextImageKHR(VkDevice device, VkSwapchainKHR sc, uint64_t timeout,
        VkSemaphore sem, VkFence fence, uint32_t *index)
{
    struct dev *d = dev_find(dkey(device));
    uint64_t t0 = now_ns();
    VkResult r = d->AcquireNextImageKHR(device, sc, timeout, sem, fence, index);
    uint64_t t1 = now_ns();
    struct swch *s = sc_find(sc);
    if (s && s->readable)
        logf_("A %llu %llu %u %d\n", (unsigned long long)t0, (unsigned long long)t1, *index, r);
    return r;
}

static uint64_t present_id_of(const VkPresentInfoKHR *info)
{
    for (const VkBaseInStructure *p = info->pNext; p; p = p->pNext)
    {
        /* VkPresentIdKHR (1000294000) and VkPresentId2KHR (1000479001) share a layout. */
        if (p->sType == (VkStructureType)1000294000 || p->sType == (VkStructureType)1000479001)
        {
            const VkPresentIdKHR *id = (const VkPresentIdKHR *)p;
            if (id->swapchainCount && id->pPresentIds)
                return id->pPresentIds[0];
        }
    }
    return 0;
}

static VKAPI_ATTR VkResult VKAPI_CALL l_QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR *info)
{
    uint64_t t_qp = now_ns();
    struct dev *d = dev_find(dkey(queue));
    if (!d)
        return VK_ERROR_DEVICE_LOST;
    struct swch *sc = info->swapchainCount == 1 ? sc_find(info->pSwapchains[0]) : NULL;
    if (!sc || !sc->readable)
        return d->QueuePresentKHR(queue, info);

    uint32_t family = queue_family(d, queue);
    if (!d->rb_ready)
    {
        if (family == UINT32_MAX || !rb_init(d, family))
        {
            sc->readable = 0;
            logf_("E readback init failed (family %u)\n", family);
            return d->QueuePresentKHR(queue, info);
        }
        d->rb_ready = 1;
    }
    if (family != d->rb_family)
        return d->QueuePresentKHR(queue, info);

    pthread_mutex_lock(&d->rb_lock);
    while (d->head - d->tail >= NSLOTS)
        pthread_cond_wait(&d->rb_cond, &d->rb_lock);
    unsigned idx = d->head % NSLOTS;
    pthread_mutex_unlock(&d->rb_lock);
    struct slot *s = &d->slots[idx];

    VkImage img = sc->images[info->pImageIndices[0]];
    d->ResetCommandBuffer(s->cmd, 0);
    VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL,
            VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, NULL};
    d->BeginCommandBuffer(s->cmd, &bi);
    VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = 0;
    b.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    b.oldLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = img;
    b.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
    b.subresourceRange.levelCount = 1;
    b.subresourceRange.layerCount = 1;
    d->CmdPipelineBarrier(s->cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0,
            NULL, 1, &b);
    static __thread VkBufferImageCopy regions[NPTS];
    for (int y = 0; y < GY; y++)
        for (int x = 0; x < GX; x++)
        {
            VkBufferImageCopy *c = &regions[y * GX + x];
            memset(c, 0, sizeof(*c));
            c->bufferOffset = ((VkDeviceSize)idx * NPTS + (VkDeviceSize)(y * GX + x)) * 4;
            c->imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
            c->imageSubresource.layerCount = 1;
            c->imageOffset.x = (int32_t)(((2 * x + 1) * sc->w) / (2 * GX));
            c->imageOffset.y = (int32_t)(((2 * y + 1) * sc->h) / (2 * GY));
            c->imageExtent.width = c->imageExtent.height = c->imageExtent.depth = 1;
        }
    d->CmdCopyImageToBuffer(s->cmd, img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, d->buf, NPTS, regions);
    b.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    b.dstAccessMask = 0;
    b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    b.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    d->CmdPipelineBarrier(s->cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL,
            0, NULL, 1, &b);
    VkMemoryBarrier hb = {VK_STRUCTURE_TYPE_MEMORY_BARRIER, NULL, VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_HOST_READ_BIT};
    d->CmdPipelineBarrier(s->cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, NULL, 0,
            NULL);
    d->EndCommandBuffer(s->cmd);

    VkPipelineStageFlags stages[16];
    uint32_t nwait = info->waitSemaphoreCount;
    for (uint32_t i = 0; i < nwait && i < 16; i++)
        stages[i] = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
    VkSubmitInfo si = {VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.waitSemaphoreCount = nwait;
    si.pWaitSemaphores = info->pWaitSemaphores;
    si.pWaitDstStageMask = stages;
    si.commandBufferCount = 1;
    si.pCommandBuffers = &s->cmd;
    si.signalSemaphoreCount = 1;
    si.pSignalSemaphores = &s->sem;
    VkResult sr = d->QueueSubmit(queue, 1, &si, s->fence);
    if (sr != VK_SUCCESS)
    {
        logf_("E submit %d\n", sr);
        return d->QueuePresentKHR(queue, info);
    }
    VkPresentInfoKHR mod = *info;
    mod.waitSemaphoreCount = 1;
    mod.pWaitSemaphores = &s->sem;
    s->sc = sc;
    s->seq = d->next_seq++;
    s->present_id = present_id_of(info);
    s->t_qp = t_qp;
    VkResult r = d->QueuePresentKHR(queue, &mod);
    s->t_qp_ret = now_ns();
    pthread_mutex_lock(&d->rb_lock);
    s->state = 1;
    d->head++;
    pthread_cond_broadcast(&d->rb_cond);
    pthread_mutex_unlock(&d->rb_lock);
    return r;
}

static VKAPI_ATTR VkResult VKAPI_CALL l_WaitForPresentKHR(VkDevice device, VkSwapchainKHR sc, uint64_t id,
        uint64_t timeout)
{
    struct dev *d = dev_find(dkey(device));
    uint64_t t0 = now_ns();
    VkResult r = d->WaitForPresentKHR(device, sc, id, timeout);
    uint64_t t1 = now_ns();
    struct swch *s = sc_find(sc);
    if (s && s->readable)
        logf_("W %llu %llu %llu %d\n", (unsigned long long)id, (unsigned long long)t0, (unsigned long long)t1, r);
    return r;
}

struct present_wait2_info
{
    VkStructureType sType;
    const void *pNext;
    uint64_t presentId;
    uint64_t timeout;
};

static VKAPI_ATTR VkResult VKAPI_CALL l_WaitForPresent2KHR(VkDevice device, VkSwapchainKHR sc, const void *info)
{
    struct dev *d = dev_find(dkey(device));
    uint64_t t0 = now_ns();
    VkResult r = d->WaitForPresent2KHR(device, sc, info);
    uint64_t t1 = now_ns();
    struct swch *s = sc_find(sc);
    if (s && s->readable)
        logf_("W %llu %llu %llu %d\n", (unsigned long long)((const struct present_wait2_info *)info)->presentId,
                (unsigned long long)t0, (unsigned long long)t1, r);
    return r;
}

/* ------------------------------------------------------------- proc addrs */
static PFN_vkVoidFunction dev_hook(const char *name, struct dev *d)
{
#define H(n) if (!strcmp(name, "vk" #n)) return (d && !d->n) ? NULL : (PFN_vkVoidFunction)l_##n;
    H(GetDeviceQueue) H(GetDeviceQueue2) H(CreateSwapchainKHR) H(DestroySwapchainKHR) H(QueuePresentKHR)
    H(AcquireNextImageKHR) H(WaitForPresentKHR) H(WaitForPresent2KHR) H(DestroyDevice)
#undef H
    return NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL inputlat_GetDeviceProcAddr(VkDevice device, const char *name)
{
    if (!strcmp(name, "vkGetDeviceProcAddr"))
        return (PFN_vkVoidFunction)inputlat_GetDeviceProcAddr;
    struct dev *d = dev_find(dkey(device));
    PFN_vkVoidFunction f = dev_hook(name, d);
    if (f)
        return f;
    return d ? d->gdpa(device, name) : NULL;
}

EXPORT VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL inputlat_GetInstanceProcAddr(VkInstance instance, const char *name)
{
    if (!strcmp(name, "vkGetInstanceProcAddr"))
        return (PFN_vkVoidFunction)inputlat_GetInstanceProcAddr;
    if (!strcmp(name, "vkCreateInstance"))
        return (PFN_vkVoidFunction)l_CreateInstance;
    if (!strcmp(name, "vkDestroyInstance"))
        return (PFN_vkVoidFunction)l_DestroyInstance;
    if (!strcmp(name, "vkCreateDevice"))
        return (PFN_vkVoidFunction)l_CreateDevice;
    if (!strcmp(name, "vkGetDeviceProcAddr"))
        return (PFN_vkVoidFunction)inputlat_GetDeviceProcAddr;
    PFN_vkVoidFunction f = dev_hook(name, NULL);
    if (f)
        return f;
    if (!instance)
        return NULL;
    struct inst *in = inst_find(dkey(instance));
    return in ? in->gipa(instance, name) : NULL;
}

EXPORT VKAPI_ATTR VkResult VKAPI_CALL inputlat_NegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface *v)
{
    if (v->loaderLayerInterfaceVersion > 2)
        v->loaderLayerInterfaceVersion = 2;
    v->pfnGetInstanceProcAddr = inputlat_GetInstanceProcAddr;
    v->pfnGetDeviceProcAddr = inputlat_GetDeviceProcAddr;
    v->pfnGetPhysicalDeviceProcAddr = NULL;
    return VK_SUCCESS;
}
