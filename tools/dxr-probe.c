/*
 * dxr-probe — standalone D3D12 ray-tracing / DX12-Ultimate capability probe.
 *
 * Purpose (issue #153, "Hardware Ray Tracing not detected on AMD (RDNA4)"):
 * report exactly what Minecraft is told about the graphics device, WITHOUT
 * starting Minecraft. "The game does not detect ray tracing" is a statement
 * about D3D12 feature bits, and those bits come from vkd3d-proton, not from
 * the launcher — so the only way to argue about them is to read them.
 *
 * It answers four questions, in the order they gate Minecraft's Ray Traced
 * graphics mode:
 *   1. which adapter does DXGI hand the game, under which name/vendor id,
 *      and which one the default D3D12 device is created on (its LUID
 *      matches one adapter's), for a laptop with two GPUs (#333);
 *   2. does D3D12 report D3D12_RAYTRACING_TIER_1_1 (Minecraft needs 1.1,
 *      not 1.0) plus shader model 6.5 and wave ops;
 *   3. does the ray-tracing path actually work, or is the tier only
 *      advertised — GetRaytracingAccelerationStructurePrebuildInfo sizes a
 *      one-triangle BLAS, which fails or returns 0 on a broken DXR stack;
 *   4. does CreateCommandSignature accept an indirect root CBV, the menu's
 *      ExecuteIndirect path that force_raw_va_cbv exists for (#27/#29/#30).
 *
 * The probe opens a GPU device, so run it the way the game runs — inside the
 * managed prefix, on the managed engine, with the launcher's VKD3D_CONFIG:
 *
 *   x86_64-w64-mingw32-gcc -O2 -Wall -Wextra -o dxr-probe.exe dxr-probe.c \
 *       -ldxguid -luuid -lole32
 *   VKD3D_CONFIG=force_raw_va_cbv \
 *   WINEPREFIX=~/.local/share/bedrock-on-linux/compatdata/pfx \
 *   PROTONPATH=~/.local/share/bedrock-on-linux/proton/GDK-Proton-xuser \
 *   PROTON_VERB=run PROTON_USE_WOW64=1 GAMEID=umu-default \
 *   ~/.local/share/bedrock-on-linux/umu/umu-run ./dxr-probe.exe
 *
 * Every line is "key: value" so the whole output can be pasted into an issue.
 * It reads no account state and sends nothing over the network.
 */

#define COBJMACROS
#define INITGUID
/* GetAdapterLuid returns a struct: the C wrapper for it needs this. */
#define WIDL_C_INLINE_WRAPPERS

#include <windows.h>
#include <initguid.h>
#include <dxgi1_6.h>
#include <d3d12.h>
#include <stdio.h>

#define PROBE_BANNER "dxr-probe-v1"

typedef HRESULT (WINAPI *pfn_CreateDXGIFactory1)(REFIID, void **);
typedef HRESULT (WINAPI *pfn_D3D12CreateDevice)(IUnknown *, D3D_FEATURE_LEVEL,
                                                REFIID, void **);
typedef HRESULT (WINAPI *pfn_D3D12SerializeRootSignature)(
        const D3D12_ROOT_SIGNATURE_DESC *, D3D_ROOT_SIGNATURE_VERSION,
        ID3DBlob **, ID3DBlob **);

static void print_env(const char *name)
{
    char value[1024];
    DWORD len = GetEnvironmentVariableA(name, value, sizeof(value));

    printf("env.%s: %s\n", name, (len && len < sizeof(value)) ? value : "");
}

static const char *raytracing_tier_name(D3D12_RAYTRACING_TIER tier)
{
    switch (tier)
    {
        case D3D12_RAYTRACING_TIER_NOT_SUPPORTED: return "not supported";
        case D3D12_RAYTRACING_TIER_1_0:           return "1.0";
        case D3D12_RAYTRACING_TIER_1_1:           return "1.1";
        default:                                  return "unknown";
    }
}

/* A game may ask DXGI for "the fast card" instead of taking adapter 0, so
 * on a laptop with two GPUs both answers matter (#333). */
static void describe_preference(IDXGIFactory1 *factory1,
                                DXGI_GPU_PREFERENCE preference,
                                const char *label)
{
    IDXGIFactory6 *factory6 = NULL;
    IDXGIAdapter1 *adapter = NULL;
    DXGI_ADAPTER_DESC1 desc;
    char name[256] = "";

    if (FAILED(IDXGIFactory1_QueryInterface(factory1, &IID_IDXGIFactory6,
                                            (void **)&factory6)))
    {
        printf("dxgi.preference.%s: no IDXGIFactory6\n", label);
        return;
    }
    if (SUCCEEDED(IDXGIFactory6_EnumAdapterByGpuPreference(factory6, 0,
            preference, &IID_IDXGIAdapter1, (void **)&adapter))
            && SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &desc)))
    {
        WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, name,
                            sizeof(name), NULL, NULL);
        printf("dxgi.preference.%s: %s\n", label, name);
    }
    else
        printf("dxgi.preference.%s: FAILED\n", label);
    if (adapter)
        IDXGIAdapter1_Release(adapter);
    IDXGIFactory6_Release(factory6);
}

static void describe_adapters(pfn_CreateDXGIFactory1 create_factory)
{
    IDXGIFactory1 *factory = NULL;
    IDXGIAdapter1 *adapter = NULL;
    HRESULT hr;
    UINT index;

    hr = create_factory(&IID_IDXGIFactory1, (void **)&factory);
    if (FAILED(hr))
    {
        printf("dxgi.factory: FAILED hr=0x%08lx\n", (unsigned long)hr);
        return;
    }

    for (index = 0; IDXGIFactory1_EnumAdapters1(factory, index, &adapter)
            != DXGI_ERROR_NOT_FOUND; ++index)
    {
        DXGI_ADAPTER_DESC1 desc;
        char name[256] = "";

        if (SUCCEEDED(IDXGIAdapter1_GetDesc1(adapter, &desc)))
        {
            WideCharToMultiByte(CP_UTF8, 0, desc.Description, -1, name,
                                sizeof(name), NULL, NULL);
            printf("adapter[%u].description: %s\n", index, name);
            printf("adapter[%u].vendor_id: 0x%04x\n", index,
                   (unsigned)desc.VendorId);
            printf("adapter[%u].device_id: 0x%04x\n", index,
                   (unsigned)desc.DeviceId);
            printf("adapter[%u].dedicated_video_memory_mb: %llu\n", index,
                   (unsigned long long)(desc.DedicatedVideoMemory >> 20));
            printf("adapter[%u].software: %s\n", index,
                   (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) ? "yes" : "no");
            printf("adapter[%u].luid: %08lx:%08lx\n", index,
                   (long)desc.AdapterLuid.HighPart,
                   (unsigned long)desc.AdapterLuid.LowPart);
        }
        IDXGIAdapter1_Release(adapter);
        adapter = NULL;
    }

    describe_preference(factory, DXGI_GPU_PREFERENCE_HIGH_PERFORMANCE,
                        "high_performance");
    describe_preference(factory, DXGI_GPU_PREFERENCE_MINIMUM_POWER,
                        "minimum_power");
    IDXGIFactory1_Release(factory);
}

/* Sizing a one-triangle bottom-level acceleration structure touches the real
 * DXR code path without needing a single shader, a command list or a heap. A
 * device that advertises a tier but cannot size a BLAS is advertising a lie. */
static void probe_acceleration_structure(ID3D12Device *device)
{
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO prebuild;
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS inputs;
    D3D12_RAYTRACING_GEOMETRY_DESC geometry;
    ID3D12Device5 *device5 = NULL;
    HRESULT hr;

    hr = ID3D12Device_QueryInterface(device, &IID_ID3D12Device5,
                                     (void **)&device5);
    if (FAILED(hr))
    {
        printf("dxr.ID3D12Device5: FAILED hr=0x%08lx\n", (unsigned long)hr);
        printf("dxr.blas_prebuild_bytes: 0\n");
        return;
    }
    printf("dxr.ID3D12Device5: ok\n");

    memset(&geometry, 0, sizeof(geometry));
    geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    geometry.Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    geometry.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    geometry.Triangles.VertexCount = 3;
    geometry.Triangles.VertexBuffer.StrideInBytes = 3 * sizeof(float);

    memset(&inputs, 0, sizeof(inputs));
    inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    inputs.Flags =
            D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_PREFER_FAST_TRACE;
    inputs.DescsLayout = D3D12_ELEMENTS_LAYOUT_ARRAY;
    inputs.NumDescs = 1;
    inputs.pGeometryDescs = &geometry;

    memset(&prebuild, 0, sizeof(prebuild));
    ID3D12Device5_GetRaytracingAccelerationStructurePrebuildInfo(
            device5, &inputs, &prebuild);
    printf("dxr.blas_prebuild_bytes: %llu\n",
           (unsigned long long)prebuild.ResultDataMaxSizeInBytes);
    printf("dxr.blas_scratch_bytes: %llu\n",
           (unsigned long long)prebuild.ScratchDataSizeInBytes);

    ID3D12Device5_Release(device5);
}

/* The main menu updates a root CBV through ExecuteIndirect. Without
 * force_raw_va_cbv this call returns E_NOTIMPL on vkd3d-proton and the menu
 * freezes (#27/#29/#30), so report it next to the ray-tracing verdict. */
static void probe_command_signature(ID3D12Device *device,
                                    pfn_D3D12SerializeRootSignature serialize)
{
    D3D12_INDIRECT_ARGUMENT_DESC arguments[2];
    D3D12_COMMAND_SIGNATURE_DESC signature_desc;
    D3D12_ROOT_SIGNATURE_DESC root_desc;
    D3D12_ROOT_PARAMETER parameter;
    ID3D12CommandSignature *signature = NULL;
    ID3D12RootSignature *root = NULL;
    ID3DBlob *blob = NULL, *error = NULL;
    HRESULT hr;

    memset(&parameter, 0, sizeof(parameter));
    parameter.ParameterType = D3D12_ROOT_PARAMETER_TYPE_CBV;
    parameter.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;

    memset(&root_desc, 0, sizeof(root_desc));
    root_desc.NumParameters = 1;
    root_desc.pParameters = &parameter;
    root_desc.Flags = D3D12_ROOT_SIGNATURE_FLAG_ALLOW_INPUT_ASSEMBLER_INPUT_LAYOUT;

    hr = serialize(&root_desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, &error);
    if (error)
        ID3D10Blob_Release(error);
    if (FAILED(hr))
    {
        printf("indirect.root_signature: FAILED hr=0x%08lx\n",
               (unsigned long)hr);
        return;
    }

    hr = ID3D12Device_CreateRootSignature(device, 0,
                                          ID3D10Blob_GetBufferPointer(blob),
                                          ID3D10Blob_GetBufferSize(blob),
                                          &IID_ID3D12RootSignature,
                                          (void **)&root);
    ID3D10Blob_Release(blob);
    if (FAILED(hr))
    {
        printf("indirect.root_signature: FAILED hr=0x%08lx\n",
               (unsigned long)hr);
        return;
    }
    printf("indirect.root_signature: ok\n");

    memset(arguments, 0, sizeof(arguments));
    arguments[0].Type = D3D12_INDIRECT_ARGUMENT_TYPE_CONSTANT_BUFFER_VIEW;
    arguments[0].ConstantBufferView.RootParameterIndex = 0;
    arguments[1].Type = D3D12_INDIRECT_ARGUMENT_TYPE_DRAW;

    memset(&signature_desc, 0, sizeof(signature_desc));
    signature_desc.ByteStride = sizeof(D3D12_GPU_VIRTUAL_ADDRESS)
            + sizeof(D3D12_DRAW_ARGUMENTS);
    signature_desc.NumArgumentDescs = 2;
    signature_desc.pArgumentDescs = arguments;

    hr = ID3D12Device_CreateCommandSignature(device, &signature_desc, root,
                                             &IID_ID3D12CommandSignature,
                                             (void **)&signature);
    printf("indirect.root_cbv_command_signature: %s hr=0x%08lx\n",
           SUCCEEDED(hr) ? "ok" : "FAILED", (unsigned long)hr);
    if (signature)
        ID3D12CommandSignature_Release(signature);
    ID3D12RootSignature_Release(root);
}

static void describe_device(ID3D12Device *device)
{
    D3D12_FEATURE_DATA_D3D12_OPTIONS options;
    D3D12_FEATURE_DATA_D3D12_OPTIONS1 options1;
    D3D12_FEATURE_DATA_D3D12_OPTIONS5 options5;
    D3D12_FEATURE_DATA_D3D12_OPTIONS6 options6;
    D3D12_FEATURE_DATA_D3D12_OPTIONS7 options7;
    D3D12_FEATURE_DATA_SHADER_MODEL shader_model;
    unsigned int model_major = 0, model_minor = 0;
    int raytracing_ready, ultimate;

    memset(&options, 0, sizeof(options));
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_D3D12_OPTIONS, &options, sizeof(options))))
        printf("d3d12.resource_binding_tier: %u\n",
               (unsigned)options.ResourceBindingTier);

    memset(&options1, 0, sizeof(options1));
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_D3D12_OPTIONS1, &options1, sizeof(options1))))
    {
        printf("d3d12.wave_ops: %s\n", options1.WaveOps ? "yes" : "no");
        printf("d3d12.int64_shader_ops: %s\n",
               options1.Int64ShaderOps ? "yes" : "no");
    }

    memset(&options5, 0, sizeof(options5));
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_D3D12_OPTIONS5, &options5, sizeof(options5))))
        printf("d3d12.raytracing_tier: %s\n",
               raytracing_tier_name(options5.RaytracingTier));
    else
        options5.RaytracingTier = D3D12_RAYTRACING_TIER_NOT_SUPPORTED;

    memset(&options6, 0, sizeof(options6));
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_D3D12_OPTIONS6, &options6, sizeof(options6))))
        printf("d3d12.variable_shading_rate_tier: %u\n",
               (unsigned)options6.VariableShadingRateTier);

    memset(&options7, 0, sizeof(options7));
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_D3D12_OPTIONS7, &options7, sizeof(options7))))
    {
        printf("d3d12.mesh_shader_tier: %u\n",
               (unsigned)options7.MeshShaderTier);
        printf("d3d12.sampler_feedback_tier: %u\n",
               (unsigned)options7.SamplerFeedbackTier);
    }

    /* CheckFeatureSupport reports the highest model at or below the one
     * asked for, so ask for the highest this header knows about. */
    memset(&shader_model, 0, sizeof(shader_model));
    shader_model.HighestShaderModel = D3D_SHADER_MODEL_6_7;
    if (SUCCEEDED(ID3D12Device_CheckFeatureSupport(device,
            D3D12_FEATURE_SHADER_MODEL, &shader_model, sizeof(shader_model))))
    {
        model_major = (unsigned)shader_model.HighestShaderModel >> 4;
        model_minor = (unsigned)shader_model.HighestShaderModel & 0xf;
        printf("d3d12.highest_shader_model: %u.%u\n", model_major,
               model_minor);
    }

    probe_acceleration_structure(device);

    /* Minecraft's Ray Traced mode needs inline ray tracing (tier 1.1) and
     * shader model 6.5; tier 1.0 alone is not enough. */
    raytracing_ready = options5.RaytracingTier >= D3D12_RAYTRACING_TIER_1_1
            && (model_major > 6 || (model_major == 6 && model_minor >= 5))
            && options1.WaveOps;
    ultimate = raytracing_ready && options7.MeshShaderTier
            && options7.SamplerFeedbackTier
            && options6.VariableShadingRateTier >= 2;

    printf("verdict.raytracing_capable: %s\n", raytracing_ready ? "yes" : "no");
    printf("verdict.directx12_ultimate: %s\n", ultimate ? "yes" : "no");
}

int main(int argc, char **argv)
{
    pfn_D3D12SerializeRootSignature serialize_root_signature;
    pfn_CreateDXGIFactory1 create_factory;
    pfn_D3D12CreateDevice create_device;
    ID3D12Device *device = NULL;
    HMODULE d3d12, dxgi;
    HRESULT hr;

    /* A console app started through Proton/pressure-vessel does not always
     * get the caller's stdout, so accept a report path: dxr-probe.exe
     * Z:\tmp\dxr.txt writes there instead. */
    if (argc > 1 && !freopen(argv[1], "w", stdout))
    {
        fprintf(stderr, "could not write the report to %s\n", argv[1]);
        return 1;
    }
    setvbuf(stdout, NULL, _IONBF, 0);

    printf("probe: %s\n", PROBE_BANNER);
    print_env("VKD3D_CONFIG");
    print_env("VKD3D_FEATURE_LEVEL");
    print_env("VKD3D_DISABLE_EXTENSIONS");

    dxgi = LoadLibraryA("dxgi.dll");
    d3d12 = LoadLibraryA("d3d12.dll");
    if (!dxgi || !d3d12)
    {
        printf("load: FAILED dxgi=%p d3d12=%p\n", (void *)dxgi, (void *)d3d12);
        return 1;
    }

    create_factory = (pfn_CreateDXGIFactory1)(void *)
            GetProcAddress(dxgi, "CreateDXGIFactory1");
    create_device = (pfn_D3D12CreateDevice)(void *)
            GetProcAddress(d3d12, "D3D12CreateDevice");
    serialize_root_signature = (pfn_D3D12SerializeRootSignature)(void *)
            GetProcAddress(d3d12, "D3D12SerializeRootSignature");
    if (!create_factory || !create_device || !serialize_root_signature)
    {
        printf("load: FAILED to resolve the D3D12/DXGI entry points\n");
        return 1;
    }

    describe_adapters(create_factory);

    /* NULL adapter: the same default DXGI hands Minecraft. */
    hr = create_device(NULL, D3D_FEATURE_LEVEL_11_0, &IID_ID3D12Device,
                       (void **)&device);
    if (FAILED(hr))
    {
        printf("d3d12.device: FAILED hr=0x%08lx\n", (unsigned long)hr);
        printf("verdict.raytracing_capable: no\n");
        return 1;
    }
    printf("d3d12.device: ok\n");
    {
        LUID luid = ID3D12Device_GetAdapterLuid(device);

        printf("d3d12.device.adapter_luid: %08lx:%08lx\n",
               (long)luid.HighPart, (unsigned long)luid.LowPart);
    }

    describe_device(device);
    probe_command_signature(device, serialize_root_signature);

    ID3D12Device_Release(device);
    return 0;
}
