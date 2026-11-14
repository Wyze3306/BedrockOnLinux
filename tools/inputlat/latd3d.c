/* latd3d — a Direct3D 12 known case for the input-latency probe, run under the
 * same Wine, DXVK dxgi and vkd3d-proton as Minecraft.
 *
 * A full-screen popup window cleared to one of two colours; every raw mouse
 * motion flips the colour on the next frame. The frame loop mirrors a D3D12
 * game: wait on the frame-latency waitable object, pump messages, simulate,
 * record, execute, Present, fence.
 *
 * Env: LAT_VSYNC=1|0  LAT_WAITABLE=1|0  LAT_MAXLAT=n  LAT_CPU_MS=x (busy work
 *      per frame)  LAT_CLIP=1|0 (hide + ClipCursor like a game in mouse look)
 *      LAT_INFLIGHT=n (GPU frames in flight)  LAT_BUFFERS=n  LAT_WINDOWED=1
 */
#define COBJMACROS
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <d3d12.h>
#include <dxgi1_6.h>
#include <stdio.h>
#include <stdlib.h>

static volatile LONG g_motion;
static int envi(const char *n, int d) { const char *v = getenv(n); return v && *v ? atoi(v) : d; }
static double envf(const char *n, double d) { const char *v = getenv(n); return v && *v ? atof(v) : d; }

static LRESULT CALLBACK wndproc(HWND h, UINT m, WPARAM w, LPARAM l)
{
    if (m == WM_INPUT)
    {
        RAWINPUT ri;
        UINT sz = sizeof(ri);
        if (GetRawInputData((HRAWINPUT)l, RID_INPUT, &ri, &sz, sizeof(RAWINPUTHEADER)) != (UINT)-1 &&
                ri.header.dwType == RIM_TYPEMOUSE && (ri.data.mouse.lLastX || ri.data.mouse.lLastY))
            g_motion = 1;
    }
    if (m == WM_DESTROY) PostQuitMessage(0);
    return DefWindowProcW(h, m, w, l);
}

#define CK(x) do { HRESULT _h = (x); if (FAILED(_h)) { fprintf(stderr, "%s = %#lx\n", #x, (unsigned long)_h); fflush(stderr); ExitProcess(1); } } while (0)

int main(void)
{
    int vsync = envi("LAT_VSYNC", 1), waitable = envi("LAT_WAITABLE", 1), maxlat = envi("LAT_MAXLAT", 1);
    int clip = envi("LAT_CLIP", 1), inflight = envi("LAT_INFLIGHT", 2), nbuf = envi("LAT_BUFFERS", 3);
    double cpu_ms = envf("LAT_CPU_MS", 0);
    if (inflight < 1 || inflight > 8) inflight = 2;
    if (nbuf < 2 || nbuf > 8) nbuf = 3;

    WNDCLASSW wc = {0};
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleW(NULL);
    wc.lpszClassName = L"LatD3D";
    wc.hCursor = LoadCursorW(NULL, (LPCWSTR)IDC_ARROW);
    wc.hbrBackground = (HBRUSH)GetStockObject(BLACK_BRUSH);
    RegisterClassW(&wc);
    int W = GetSystemMetrics(SM_CXSCREEN), H = GetSystemMetrics(SM_CYSCREEN);
    fprintf(stderr, "screen %dx%d\n", W, H);
    if (W <= 0 || H <= 0) W = 1600, H = 900;
    HWND hwnd = envi("LAT_WINDOWED", 0)
            ? CreateWindowW(L"LatD3D", L"latd3d", WS_OVERLAPPEDWINDOW | WS_VISIBLE, 0, 0, 1280, 720, NULL, NULL, wc.hInstance, NULL)
            : CreateWindowW(L"LatD3D", L"latd3d", WS_POPUP | WS_VISIBLE, 0, 0, W, H, NULL, NULL, wc.hInstance, NULL);
    ShowWindow(hwnd, SW_SHOW);
    SetForegroundWindow(hwnd);
    RAWINPUTDEVICE rid = {1, 2, 0, hwnd};
    RegisterRawInputDevices(&rid, 1, sizeof(rid));
    RECT rc;
    GetClientRect(hwnd, &rc);
    UINT cw = rc.right - rc.left, ch = rc.bottom - rc.top;
    fprintf(stderr, "client %ux%u hwnd %p err %lu\n", cw, ch, (void *)hwnd, GetLastError());
    fflush(stderr);
    if (!cw || !ch) cw = W, ch = H;

    ID3D12Device *dev;
    CK(D3D12CreateDevice(NULL, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device, (void **)&dev));
    D3D12_COMMAND_QUEUE_DESC qd = {D3D12_COMMAND_LIST_TYPE_DIRECT, 0, D3D12_COMMAND_QUEUE_FLAG_NONE, 0};
    ID3D12CommandQueue *queue;
    CK(ID3D12Device_CreateCommandQueue(dev, &qd, &IID_ID3D12CommandQueue, (void **)&queue));
    IDXGIFactory5 *fac;
    CK(CreateDXGIFactory2(0, &IID_IDXGIFactory5, (void **)&fac));
    BOOL tearing = FALSE;
    IDXGIFactory5_CheckFeatureSupport(fac, DXGI_FEATURE_PRESENT_ALLOW_TEARING, &tearing, sizeof(tearing));
    DXGI_SWAP_CHAIN_DESC1 sd = {0};
    sd.Width = cw;
    sd.Height = ch;
    sd.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    sd.SampleDesc.Count = 1;
    sd.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    sd.BufferCount = nbuf;
    sd.SwapEffect = DXGI_SWAP_EFFECT_FLIP_DISCARD;
    sd.Flags = (waitable ? DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT : 0) |
            (tearing && !vsync ? DXGI_SWAP_CHAIN_FLAG_ALLOW_TEARING : 0);
    IDXGISwapChain1 *sc1;
    CK(IDXGIFactory5_CreateSwapChainForHwnd(fac, (IUnknown *)queue, hwnd, &sd, NULL, NULL, &sc1));
    IDXGISwapChain3 *sc;
    CK(IDXGISwapChain1_QueryInterface(sc1, &IID_IDXGISwapChain3, (void **)&sc));
    HANDLE latency_handle = NULL;
    if (waitable)
    {
        CK(IDXGISwapChain3_SetMaximumFrameLatency(sc, maxlat));
        latency_handle = IDXGISwapChain3_GetFrameLatencyWaitableObject(sc);
    }
    fprintf(stderr, "latd3d %ux%u vsync=%d waitable=%d maxlat=%d tearing=%d cpu=%.1fms inflight=%d buffers=%d clip=%d\n",
            cw, ch, vsync, waitable, maxlat, tearing, cpu_ms, inflight, nbuf, clip);
    fflush(stderr);

    D3D12_DESCRIPTOR_HEAP_DESC hd = {D3D12_DESCRIPTOR_HEAP_TYPE_RTV, (UINT)nbuf, D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    ID3D12DescriptorHeap *heap;
    CK(ID3D12Device_CreateDescriptorHeap(dev, &hd, &IID_ID3D12DescriptorHeap, (void **)&heap));
    UINT inc = ID3D12Device_GetDescriptorHandleIncrementSize(dev, D3D12_DESCRIPTOR_HEAP_TYPE_RTV);
    D3D12_CPU_DESCRIPTOR_HANDLE rtv0;
    heap->lpVtbl->GetCPUDescriptorHandleForHeapStart(heap, &rtv0);
    ID3D12Resource *bufs[8];
    for (int i = 0; i < nbuf; i++)
    {
        CK(IDXGISwapChain3_GetBuffer(sc, i, &IID_ID3D12Resource, (void **)&bufs[i]));
        D3D12_CPU_DESCRIPTOR_HANDLE h = {rtv0.ptr + (SIZE_T)i * inc};
        ID3D12Device_CreateRenderTargetView(dev, bufs[i], NULL, h);
    }
    ID3D12CommandAllocator *alloc[8];
    for (int i = 0; i < inflight; i++)
        CK(ID3D12Device_CreateCommandAllocator(dev, D3D12_COMMAND_LIST_TYPE_DIRECT, &IID_ID3D12CommandAllocator, (void **)&alloc[i]));
    ID3D12GraphicsCommandList *list;
    CK(ID3D12Device_CreateCommandList(dev, 0, D3D12_COMMAND_LIST_TYPE_DIRECT, alloc[0], NULL, &IID_ID3D12GraphicsCommandList, (void **)&list));
    ID3D12GraphicsCommandList_Close(list);
    ID3D12Fence *fence;
    CK(ID3D12Device_CreateFence(dev, 0, D3D12_FENCE_FLAG_NONE, &IID_ID3D12Fence, (void **)&fence));
    HANDLE fev = CreateEventW(NULL, FALSE, FALSE, NULL);
    UINT64 fval[8] = {0}, next = 1;

    if (clip)
    {
        ShowCursor(FALSE);
        RECT wr;
        GetWindowRect(hwnd, &wr);
        ClipCursor(&wr);
    }
    LARGE_INTEGER qf;
    QueryPerformanceFrequency(&qf);
    int colour = 0;
    for (UINT64 frame = 0;; frame++)
    {
        if (latency_handle)
            WaitForSingleObjectEx(latency_handle, 1000, TRUE);
        MSG msg;
        while (PeekMessageW(&msg, NULL, 0, 0, PM_REMOVE))
        {
            if (msg.message == WM_QUIT) return 0;
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        if (InterlockedExchange(&g_motion, 0))
            colour ^= 1;
        if (cpu_ms > 0)
        {
            LARGE_INTEGER a, b;
            QueryPerformanceCounter(&a);
            do QueryPerformanceCounter(&b); while ((b.QuadPart - a.QuadPart) * 1000.0 / qf.QuadPart < cpu_ms);
        }
        int slot = (int)(frame % (UINT64)inflight);
        if (ID3D12Fence_GetCompletedValue(fence) < fval[slot])
        {
            ID3D12Fence_SetEventOnCompletion(fence, fval[slot], fev);
            WaitForSingleObject(fev, INFINITE);
        }
        ID3D12CommandAllocator_Reset(alloc[slot]);
        ID3D12GraphicsCommandList_Reset(list, alloc[slot], NULL);
        UINT bi = IDXGISwapChain3_GetCurrentBackBufferIndex(sc);
        D3D12_RESOURCE_BARRIER b = {0};
        b.Type = D3D12_RESOURCE_BARRIER_TYPE_TRANSITION;
        b.Transition.pResource = bufs[bi];
        b.Transition.Subresource = D3D12_RESOURCE_BARRIER_ALL_SUBRESOURCES;
        b.Transition.StateBefore = D3D12_RESOURCE_STATE_PRESENT;
        b.Transition.StateAfter = D3D12_RESOURCE_STATE_RENDER_TARGET;
        ID3D12GraphicsCommandList_ResourceBarrier(list, 1, &b);
        D3D12_CPU_DESCRIPTOR_HANDLE h = {rtv0.ptr + (SIZE_T)bi * inc};
        float c[4] = {colour ? 0.95f : 0.05f, colour ? 0.9f : 0.1f, colour ? 0.85f : 0.15f, 1};
        ID3D12GraphicsCommandList_ClearRenderTargetView(list, h, c, 0, NULL);
        b.Transition.StateBefore = D3D12_RESOURCE_STATE_RENDER_TARGET;
        b.Transition.StateAfter = D3D12_RESOURCE_STATE_PRESENT;
        ID3D12GraphicsCommandList_ResourceBarrier(list, 1, &b);
        ID3D12GraphicsCommandList_Close(list);
        ID3D12CommandList *lists[] = {(ID3D12CommandList *)list};
        ID3D12CommandQueue_ExecuteCommandLists(queue, 1, lists);
        IDXGISwapChain3_Present(sc, vsync ? 1 : 0, (!vsync && tearing) ? DXGI_PRESENT_ALLOW_TEARING : 0);
        if (frame % 120 == 0) { fprintf(stderr, "frame %llu\n", (unsigned long long)frame); fflush(stderr); }
        fval[slot] = next++;
        ID3D12CommandQueue_Signal(queue, fence, fval[slot]);
    }
}
