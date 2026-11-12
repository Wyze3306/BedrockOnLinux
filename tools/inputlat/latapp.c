/* latapp — a known case for the input-latency probe.
 *
 * An X11 window cleared by Vulkan to one of two colours. Every pointer motion
 * (XI2 raw motion or core motion) flips the colour, after LATAPP_DELAY_MS.
 * The app logs its own view to stdout, on CLOCK_MONOTONIC:
 *   E <t_read_ns> <kind>      motion read from the X connection
 *   Q <frame> <t_qp_ns> <colour> <flipped>
 * Env: LATAPP_MODE=fifo|mailbox|immediate, LATAPP_IMAGES=n, LATAPP_DELAY_MS=d,
 *      LATAPP_INFLIGHT=n (frames the CPU may run ahead, default 2)
 */
#define VK_USE_PLATFORM_XLIB_KHR
#include <X11/Xlib.h>
#include <X11/extensions/XInput2.h>
#include <vulkan/vulkan.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <pthread.h>

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static VkDevice g_dev; static VkSwapchainKHR g_sc; static PFN_vkWaitForPresentKHR g_wait;
static pthread_mutex_t g_m = PTHREAD_MUTEX_INITIALIZER; static pthread_cond_t g_c = PTHREAD_COND_INITIALIZER;
static uint64_t g_submitted;
static void *waiter(void *a)
{
    (void)a;
    for (uint64_t id = 1;; id++)
    {
        pthread_mutex_lock(&g_m);
        while (g_submitted < id) pthread_cond_wait(&g_c, &g_m);
        pthread_mutex_unlock(&g_m);
        g_wait(g_dev, g_sc, id, UINT64_MAX);
        fprintf(stderr, "D %llu %llu\n", (unsigned long long)id, (unsigned long long)now_ns());
    }
    return NULL;
}

#define CK(x) do { VkResult _r = (x); if (_r != VK_SUCCESS) { fprintf(stderr, "%s = %d\n", #x, _r); exit(1); } } while (0)

int main(void)
{
    const char *mode_s = getenv("LATAPP_MODE");
    VkPresentModeKHR mode = VK_PRESENT_MODE_FIFO_KHR;
    if (mode_s && !strcmp(mode_s, "mailbox")) mode = VK_PRESENT_MODE_MAILBOX_KHR;
    if (mode_s && !strcmp(mode_s, "immediate")) mode = VK_PRESENT_MODE_IMMEDIATE_KHR;
    uint32_t nimages_req = getenv("LATAPP_IMAGES") ? (uint32_t)atoi(getenv("LATAPP_IMAGES")) : 3;
    uint64_t delay_ns = getenv("LATAPP_DELAY_MS") ? (uint64_t)(atof(getenv("LATAPP_DELAY_MS")) * 1e6) : 0;
    int inflight = getenv("LATAPP_INFLIGHT") ? atoi(getenv("LATAPP_INFLIGHT")) : 2;
    if (inflight < 1 || inflight > 8) inflight = 2;

    Display *dpy = XOpenDisplay(NULL);
    if (!dpy) { fprintf(stderr, "no display\n"); return 1; }
    int scr = DefaultScreen(dpy);
    int W = DisplayWidth(dpy, scr), H = DisplayHeight(dpy, scr);
    Window win = XCreateSimpleWindow(dpy, RootWindow(dpy, scr), 0, 0, W, H, 0, 0, 0);
    XSelectInput(dpy, win, PointerMotionMask | StructureNotifyMask);
    XStoreName(dpy, win, "latapp");
    XMapWindow(dpy, win);
    int xi_op, ev, err;
    if (XQueryExtension(dpy, "XInputExtension", &xi_op, &ev, &err))
    {
        XIEventMask m;
        unsigned char bits[XIMaskLen(XI_LASTEVENT)] = {0};
        XISetMask(bits, XI_RawMotion);
        m.deviceid = XIAllMasterDevices;
        m.mask_len = sizeof(bits);
        m.mask = bits;
        XISelectEvents(dpy, RootWindow(dpy, scr), &m, 1);
    }
    XFlush(dpy);
    for (;;) { XEvent e; XNextEvent(dpy, &e); if (e.type == MapNotify) break; }

    const char *iexts[] = {VK_KHR_SURFACE_EXTENSION_NAME, VK_KHR_XLIB_SURFACE_EXTENSION_NAME};
    VkApplicationInfo app = {VK_STRUCTURE_TYPE_APPLICATION_INFO, NULL, "latapp", 1, NULL, 0, VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici = {VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, NULL, 0, &app, 0, NULL, 2, iexts};
    VkInstance inst; CK(vkCreateInstance(&ici, NULL, &inst));
    VkXlibSurfaceCreateInfoKHR xsci = {VK_STRUCTURE_TYPE_XLIB_SURFACE_CREATE_INFO_KHR, NULL, 0, dpy, win};
    VkSurfaceKHR surf; CK(vkCreateXlibSurfaceKHR(inst, &xsci, NULL, &surf));
    uint32_t n = 8; VkPhysicalDevice pds[8]; CK(vkEnumeratePhysicalDevices(inst, &n, pds));
    VkPhysicalDevice pd = VK_NULL_HANDLE; uint32_t qf = 0;
    for (uint32_t i = 0; i < n && !pd; i++)
    {
        VkPhysicalDeviceProperties pr; vkGetPhysicalDeviceProperties(pds[i], &pr);
        if (pr.deviceType != VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) continue;
        uint32_t nq = 16; VkQueueFamilyProperties qp[16]; vkGetPhysicalDeviceQueueFamilyProperties(pds[i], &nq, qp);
        for (uint32_t q = 0; q < nq; q++)
        {
            VkBool32 sup = 0; vkGetPhysicalDeviceSurfaceSupportKHR(pds[i], q, surf, &sup);
            if ((qp[q].queueFlags & VK_QUEUE_GRAPHICS_BIT) && sup) { pd = pds[i]; qf = q; fprintf(stderr, "gpu %s\n", pr.deviceName); break; }
        }
    }
    if (!pd) { fprintf(stderr, "no gpu\n"); return 1; }
    float prio = 1;
    VkDeviceQueueCreateInfo qci = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, NULL, 0, qf, 1, &prio};
    const char *dexts[] = {VK_KHR_SWAPCHAIN_EXTENSION_NAME, VK_KHR_PRESENT_ID_EXTENSION_NAME, VK_KHR_PRESENT_WAIT_EXTENSION_NAME};
    int pw = getenv("LATAPP_PRESENTWAIT") && atoi(getenv("LATAPP_PRESENTWAIT"));
    VkPhysicalDevicePresentWaitFeaturesKHR pwf = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR, NULL, VK_TRUE};
    VkPhysicalDevicePresentIdFeaturesKHR pif = {VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR, &pwf, VK_TRUE};
    VkDeviceCreateInfo dci = {VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, pw ? &pif : NULL, 0, 1, &qci, 0, NULL, pw ? 3 : 1, dexts, NULL};
    VkDevice dev; CK(vkCreateDevice(pd, &dci, NULL, &dev));
    VkQueue q; vkGetDeviceQueue(dev, qf, 0, &q);

    VkSurfaceCapabilitiesKHR caps; CK(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(pd, surf, &caps));
    VkSwapchainCreateInfoKHR sci = {VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR};
    sci.surface = surf;
    sci.minImageCount = nimages_req < caps.minImageCount ? caps.minImageCount : nimages_req;
    sci.imageFormat = VK_FORMAT_B8G8R8A8_UNORM;
    sci.imageColorSpace = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
    sci.imageExtent = caps.currentExtent.width == 0xFFFFFFFF ? (VkExtent2D){(uint32_t)W, (uint32_t)H} : caps.currentExtent;
    sci.imageArrayLayers = 1;
    sci.imageUsage = VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT;
    sci.preTransform = VK_SURFACE_TRANSFORM_IDENTITY_BIT_KHR;
    sci.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    sci.presentMode = mode;
    sci.clipped = VK_TRUE;
    VkSwapchainKHR sc; CK(vkCreateSwapchainKHR(dev, &sci, NULL, &sc));
    uint32_t ni = 0; vkGetSwapchainImagesKHR(dev, sc, &ni, NULL);
    VkImage imgs[16]; if (ni > 16) ni = 16; vkGetSwapchainImagesKHR(dev, sc, &ni, imgs);
    fprintf(stderr, "swapchain %ux%u images=%u mode=%d delay=%.1fms inflight=%d\n", sci.imageExtent.width,
            sci.imageExtent.height, ni, mode, delay_ns / 1e6, inflight);

    if (pw)
    {
        g_dev = dev; g_sc = sc; g_wait = (PFN_vkWaitForPresentKHR)vkGetDeviceProcAddr(dev, "vkWaitForPresentKHR");
        pthread_t th; pthread_create(&th, NULL, waiter, NULL);
    }
    VkCommandPoolCreateInfo pci = {VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, NULL, VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, qf};
    VkCommandPool pool; CK(vkCreateCommandPool(dev, &pci, NULL, &pool));
    VkCommandBuffer cmd[8]; VkFence fence[8]; VkSemaphore acq[8], done[16];
    VkCommandBufferAllocateInfo cai = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, NULL, pool, VK_COMMAND_BUFFER_LEVEL_PRIMARY, 8};
    CK(vkAllocateCommandBuffers(dev, &cai, cmd));
    VkFenceCreateInfo fci = {VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, NULL, VK_FENCE_CREATE_SIGNALED_BIT};
    VkSemaphoreCreateInfo sei = {VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO, NULL, 0};
    for (int i = 0; i < 8; i++) { CK(vkCreateFence(dev, &fci, NULL, &fence[i])); CK(vkCreateSemaphore(dev, &sei, NULL, &acq[i])); }
    for (int i = 0; i < 16; i++) CK(vkCreateSemaphore(dev, &sei, NULL, &done[i]));

    int colour = 0, pending = 0, flipped_unshown = 0;
    uint64_t pending_at = 0;
    for (uint64_t frame = 0;; frame++)
    {
        int f = (int)(frame % (uint64_t)inflight);
        CK(vkWaitForFences(dev, 1, &fence[f], VK_TRUE, UINT64_MAX));
        /* Read input at the start of the frame, as a game does. */
        while (XPending(dpy))
        {
            XEvent e; XNextEvent(dpy, &e);
            int motion = 0; const char *kind = "";
            if (e.type == MotionNotify) motion = 1, kind = "core";
            else if (e.type == GenericEvent && e.xcookie.extension == xi_op && XGetEventData(dpy, &e.xcookie))
            {
                if (e.xcookie.evtype == XI_RawMotion) motion = 1, kind = "raw";
                XFreeEventData(dpy, &e.xcookie);
            }
            if (motion)
            {
                uint64_t t = now_ns();
                printf("E %llu %s\n", (unsigned long long)t, kind);
                if (!pending) { pending = 1; pending_at = t + delay_ns; }
            }
        }
        if (pending && now_ns() >= pending_at) { colour ^= 1; pending = 0; flipped_unshown = 1; }
        uint32_t idx;
        VkResult ar = vkAcquireNextImageKHR(dev, sc, UINT64_MAX, acq[f], VK_NULL_HANDLE, &idx);
        if (ar != VK_SUCCESS && ar != VK_SUBOPTIMAL_KHR) { fprintf(stderr, "acquire %d\n", ar); return 1; }
        CK(vkResetFences(dev, 1, &fence[f]));
        vkResetCommandBuffer(cmd[f], 0);
        VkCommandBufferBeginInfo bi = {VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, NULL, VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, NULL};
        vkBeginCommandBuffer(cmd[f], &bi);
        VkImageMemoryBarrier b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
        b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED; b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = imgs[idx];
        b.subresourceRange = (VkImageSubresourceRange){VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        vkCmdPipelineBarrier(cmd[f], VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 0, NULL, 1, &b);
        VkClearColorValue c = {{colour ? 0.95f : 0.05f, colour ? 0.9f : 0.1f, colour ? 0.85f : 0.15f, 1}};
        vkCmdClearColorImage(cmd[f], imgs[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &c, 1, &b.subresourceRange);
        b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT; b.dstAccessMask = 0;
        b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL; b.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
        vkCmdPipelineBarrier(cmd[f], VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, NULL, 0, NULL, 1, &b);
        vkEndCommandBuffer(cmd[f]);
        VkPipelineStageFlags st = VK_PIPELINE_STAGE_TRANSFER_BIT;
        VkSubmitInfo si = {VK_STRUCTURE_TYPE_SUBMIT_INFO, NULL, 1, &acq[f], &st, 1, &cmd[f], 1, &done[idx]};
        CK(vkQueueSubmit(q, 1, &si, fence[f]));
        uint64_t pid = frame + 1;
        VkPresentIdKHR pidi = {VK_STRUCTURE_TYPE_PRESENT_ID_KHR, NULL, 1, &pid};
        VkPresentInfoKHR pi = {VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, pw ? &pidi : NULL, 1, &done[idx], 1, &sc, &idx, NULL};
        uint64_t t = now_ns();
        VkResult pr = vkQueuePresentKHR(q, &pi);
        if (pw) { pthread_mutex_lock(&g_m); g_submitted = pid; pthread_cond_signal(&g_c); pthread_mutex_unlock(&g_m); }
        printf("Q %llu %llu %d %d\n", (unsigned long long)frame, (unsigned long long)t, colour, flipped_unshown);
        fflush(stdout);
        flipped_unshown = 0;
        if (pr != VK_SUCCESS && pr != VK_SUBOPTIMAL_KHR) { fprintf(stderr, "present %d\n", pr); return 1; }
    }
}
