/* SPDX-License-Identifier: MIT
 * x64 probe for the experimental ARM64EC engine's smoke test (#250). Built as
 * an ordinary x86-64 program, it must run through FEX (libarm64ecfex.dll) and
 * reach our ARM64X modules from emulated code, the way Minecraft does. */
#include <windows.h>
#include <stdio.h>

typedef BOOL (WINAPI *wow64proc2)(HANDLE, USHORT *, USHORT *);
typedef HRESULT (WINAPI *create_device)(IUnknown *, int, REFIID, void **);

static const GUID iid_device = {0x189819f1, 0x1db6, 0x4b57,
                                {0xbe, 0x54, 0x18, 0x21, 0x33, 0x9b, 0x85, 0xf7}};

int main(void)
{
    USHORT process = 0, native = 0;
    HMODULE kernel32 = GetModuleHandleA("kernel32.dll");
    wow64proc2 is_wow64 = (wow64proc2)GetProcAddress(kernel32, "IsWow64Process2");
    HMODULE xgdk, d3d12;
    int failures = 0;

    if (is_wow64 && is_wow64(GetCurrentProcess(), &process, &native))
        printf("probe: process machine %#x, native machine %#x\n", process, native);
    if (native != IMAGE_FILE_MACHINE_ARM64) {
        printf("probe: FAIL not running on an ARM64 Wine\n");
        failures++;
    }

    xgdk = LoadLibraryA("xgameruntime.dll");
    if (!xgdk || !GetProcAddress(xgdk, "QueryApiImpl")) {
        printf("probe: FAIL xgameruntime.dll / QueryApiImpl: error %lu\n", GetLastError());
        failures++;
    } else {
        printf("probe: xgameruntime.dll loaded, QueryApiImpl resolved\n");
    }

    d3d12 = LoadLibraryA("d3d12.dll");
    if (!d3d12) {
        printf("probe: FAIL d3d12.dll: error %lu\n", GetLastError());
        failures++;
    } else {
        create_device create = (create_device)GetProcAddress(d3d12, "D3D12CreateDevice");
        void *device = NULL;
        HRESULT hr = create ? create(NULL, 0xb000 /* 11_0 */, &iid_device, &device) : E_NOINTERFACE;
        /* A runner has no GPU: lavapipe gives a device, nothing gives an
         * error. Either is an answer; a crash is not. */
        printf("probe: D3D12CreateDevice -> %#lx%s\n", (unsigned long)hr,
               device ? " (device created)" : "");
    }

    printf("probe: %s\n", failures ? "FAILED" : "OK");
    return failures ? 1 : 0;
}
