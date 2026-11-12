/* bol levi-loader — start LeviLamina inside Minecraft under Wine.
 *
 * On Windows, LeviLamina's installer (lip + PeEditor) adds PreLoader.dll to
 * the import table of Minecraft.Windows.exe, so the loader starts before the
 * game's own code runs. The executable BedrockOnLinux downloads from the
 * Store stays encrypted on disk (Wine decrypts it while mapping it), so its
 * import table cannot be edited.
 *
 * This DLL is installed next to the game as vcruntime140_1.dll, which the
 * game imports and does not ship: Wine finds it in the application
 * directory before the runtime in system32 and maps it while it resolves the
 * game's imports, before the entry point runs -- where PeEditor's import
 * would have been. It forwards the runtime's three exports to the real
 * vcruntime140_1.dll from system32, then loads PreLoader.dll from the game
 * directory, which loads LeviLamina and the mods under mods\.
 *
 * It only loads PreLoader into Minecraft.Windows.exe, and only when
 * PreLoader.dll is there. What happened is written to mods\bol-loader.log
 * for the launcher to show.
 *
 * With mods loaded, it also stands in for one thing Proton's DXGI (DXVK)
 * lacks: IDXGIFactory2::CreateSwapChainForComposition answers E_NOTIMPL.
 * Mods that draw over the game (Playback's replay editor) create a small
 * composition swap chain only to find the addresses of the swap chain's
 * methods, and without it they install none of their hooks -- the editor
 * then opens invisible and holds the game's input. When DXVK refuses, the
 * same swap chain is made on a hidden window: the same class, the same
 * methods, which is all such a probe looks at. Real composition (a swap
 * chain with no window, for a compositor) does not exist under Wine anyway.
 *
 * Build: x86_64-w64-mingw32-gcc -O2 -shared -s -nostartfiles \
 *            -Wl,--no-insert-timestamp -Wl,-e,DllMain levi-loader.c \
 *            -o ../bol/levi-loader.dll -lkernel32 -luser32
 */
#include <windows.h>
#include <dxgi1_2.h>

void *bol_real_CxxFrameHandler4;
void *bol_real_NLG_Dispatch2;
void *bol_real_NLG_Return2;

/* The exports jump straight to the real runtime: they keep every register
 * and the stack exactly as the caller left them. */
__asm__(
    ".text\n"
    ".globl __CxxFrameHandler4\n"
    "__CxxFrameHandler4:\n"
    "    jmp *bol_real_CxxFrameHandler4(%rip)\n"
    ".globl __NLG_Dispatch2\n"
    "__NLG_Dispatch2:\n"
    "    jmp *bol_real_NLG_Dispatch2(%rip)\n"
    ".globl __NLG_Return2\n"
    "__NLG_Return2:\n"
    "    jmp *bol_real_NLG_Return2(%rip)\n"
    ".section .drectve\n"
    ".ascii \" -export:__CxxFrameHandler4 -export:__NLG_Dispatch2"
    " -export:__NLG_Return2\"\n"
    ".text\n");

static void append_log(const wchar_t *dir, const char *text)
{
    wchar_t path[MAX_PATH * 2];
    HANDLE h;
    DWORD written;

    if (lstrlenW(dir) + 32 >= (int)(sizeof(path) / sizeof(path[0])))
        return;
    lstrcpyW(path, dir);
    lstrcatW(path, L"mods\\bol-loader.log");
    h = CreateFileW(path, FILE_APPEND_DATA, FILE_SHARE_READ | FILE_SHARE_WRITE,
                    NULL, OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE)
        return;
    WriteFile(h, text, (DWORD)lstrlenA(text), &written, NULL);
    WriteFile(h, "\r\n", 2, &written, NULL);
    CloseHandle(h);
}

static void log_error(const wchar_t *dir, const char *what, DWORD code)
{
    char line[160];
    char digits[12];
    int n = 0, len;

    do {
        digits[n++] = (char)('0' + code % 10);
        code /= 10;
    } while (code && n < 11);
    len = lstrlenA(what);
    if (len > 120)
        len = 120;
    CopyMemory(line, what, len);
    lstrcpyA(line + len, " (error ");
    len += 8;
    while (n)
        line[len++] = digits[--n];
    line[len++] = ')';
    line[len] = 0;
    append_log(dir, line);
}

static BOOL resolve_runtime(HINSTANCE self)
{
    wchar_t path[MAX_PATH];
    UINT n = GetSystemDirectoryW(path, MAX_PATH);
    HMODULE real;

    if (n == 0 || n + 24 >= MAX_PATH)
        return FALSE;
    lstrcatW(path, L"\\vcruntime140_1.dll");
    real = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!real || real == (HMODULE)self)
        return FALSE;
    bol_real_CxxFrameHandler4 = (void *)GetProcAddress(real, "__CxxFrameHandler4");
    bol_real_NLG_Dispatch2 = (void *)GetProcAddress(real, "__NLG_Dispatch2");
    bol_real_NLG_Return2 = (void *)GetProcAddress(real, "__NLG_Return2");
    return bol_real_CxxFrameHandler4 != NULL;
}

/* The directory of the running executable, with its trailing backslash, and
 * whether that executable is Minecraft.Windows.exe. */
static BOOL game_directory(wchar_t *dir, DWORD cap)
{
    DWORD n = GetModuleFileNameW(NULL, dir, cap);
    wchar_t *name;

    if (n == 0 || n >= cap)
        return FALSE;
    name = dir + n;
    while (name > dir && name[-1] != L'\\' && name[-1] != L'/')
        --name;
    if (lstrcmpiW(name, L"Minecraft.Windows.exe") != 0)
        return FALSE;
    *name = 0;
    return TRUE;
}

/* ---- CreateSwapChainForComposition stand-in --------------------------- */

#define FACTORY_CREATE_FOR_HWND 15
#define FACTORY_CREATE_FOR_COMPOSITION 24

typedef HRESULT(STDMETHODCALLTYPE *for_hwnd_fn)(
    IDXGIFactory2 *, IUnknown *, HWND, const DXGI_SWAP_CHAIN_DESC1 *,
    const DXGI_SWAP_CHAIN_FULLSCREEN_DESC *, IDXGIOutput *, IDXGISwapChain1 **);
typedef HRESULT(STDMETHODCALLTYPE *for_composition_fn)(
    IDXGIFactory2 *, IUnknown *, const DXGI_SWAP_CHAIN_DESC1 *, IDXGIOutput *,
    IDXGISwapChain1 **);
typedef HRESULT(WINAPI *create_factory_fn)(REFIID, void **);

static for_hwnd_fn real_for_hwnd;
static for_composition_fn real_for_composition;
static HWND stand_in_window;
static wchar_t game_dir_for_log[MAX_PATH * 2];

/* {50c83a1c-e072-4c48-87b0-3630fa36a6d0} */
static const GUID bol_IID_IDXGIFactory2 = {
    0x50c83a1c, 0xe072, 0x4c48, {0x87, 0xb0, 0x36, 0x30, 0xfa, 0x36, 0xa6, 0xd0}};

static HRESULT STDMETHODCALLTYPE composition_stand_in(
    IDXGIFactory2 *factory, IUnknown *device, const DXGI_SWAP_CHAIN_DESC1 *desc,
    IDXGIOutput *output, IDXGISwapChain1 **swap_chain)
{
    HRESULT hr = real_for_composition(factory, device, desc, output, swap_chain);
    DXGI_SWAP_CHAIN_DESC1 copy;

    if ((hr != E_NOTIMPL && hr != DXGI_ERROR_UNSUPPORTED) || !desc || !swap_chain)
        return hr;
    if (!stand_in_window) {
        stand_in_window = CreateWindowExW(
            WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, L"STATIC",
            L"bol composition stand-in", WS_POPUP, 0, 0,
            desc->Width ? (int)desc->Width : 8,
            desc->Height ? (int)desc->Height : 8, NULL, NULL, NULL, NULL);
        if (!stand_in_window)
            return hr;
    }
    copy = *desc;
    /* A window's swap chain takes no premultiplied alpha. */
    copy.AlphaMode = DXGI_ALPHA_MODE_UNSPECIFIED;
    return real_for_hwnd(factory, device, stand_in_window, &copy, NULL, output,
                         swap_chain);
}

static DWORD WINAPI install_composition_stand_in(void *unused)
{
    HMODULE dxgi = LoadLibraryW(L"dxgi.dll");
    create_factory_fn create;
    IDXGIFactory2 *factory = NULL;
    void **vtable;
    DWORD old;

    (void)unused;
    if (!dxgi)
        return 1;
    create = (create_factory_fn)(void *)GetProcAddress(dxgi, "CreateDXGIFactory1");
    if (!create || FAILED(create(&bol_IID_IDXGIFactory2, (void **)&factory))
        || !factory)
        return 1;
    /* Every factory DXGI hands out shares this table: patching it once
     * covers the probe a mod makes later. */
    vtable = *(void ***)factory;
    real_for_hwnd = (for_hwnd_fn)vtable[FACTORY_CREATE_FOR_HWND];
    real_for_composition = (for_composition_fn)vtable[FACTORY_CREATE_FOR_COMPOSITION];
    if (real_for_composition != composition_stand_in
        && VirtualProtect(&vtable[FACTORY_CREATE_FOR_COMPOSITION], sizeof(void *),
                          PAGE_READWRITE, &old)) {
        InterlockedExchangePointer(&vtable[FACTORY_CREATE_FOR_COMPOSITION],
                                   (void *)composition_stand_in);
        VirtualProtect(&vtable[FACTORY_CREATE_FOR_COMPOSITION], sizeof(void *),
                       old, &old);
        append_log(game_dir_for_log,
                   "bol levi-loader: composition swap chains stand in through "
                   "a hidden window");
    }
    factory->lpVtbl->Release(factory);
    return 0;
}

static void load_preloader(void)
{
    wchar_t dir[MAX_PATH * 2];
    wchar_t preloader[MAX_PATH * 2];
    HMODULE module;

    if (!game_directory(dir, MAX_PATH * 2))
        return;
    if (lstrlenW(dir) + 16 >= MAX_PATH * 2)
        return;
    lstrcpyW(preloader, dir);
    lstrcatW(preloader, L"PreLoader.dll");
    if (GetFileAttributesW(preloader) == INVALID_FILE_ATTRIBUTES)
        return;
    append_log(dir, "bol levi-loader: loading PreLoader.dll");
    module = LoadLibraryExW(preloader, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (module) {
        HANDLE thread;
        append_log(dir, "bol levi-loader: PreLoader.dll loaded");
        /* Not from here: making a DXGI factory inside DllMain, under the
         * loader lock, is not allowed. The thread starts once the game's
         * imports are done, long before mods are enabled. */
        lstrcpyW(game_dir_for_log, dir);
        thread = CreateThread(NULL, 0, install_composition_stand_in, NULL, 0, NULL);
        if (thread)
            CloseHandle(thread);
    }
    else
        log_error(dir, "bol levi-loader: PreLoader.dll failed to load",
                  GetLastError());
}

BOOL WINAPI DllMain(HINSTANCE self, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason != DLL_PROCESS_ATTACH)
        return TRUE;
    DisableThreadLibraryCalls(self);
    if (!resolve_runtime(self)) {
        OutputDebugStringA("bol levi-loader: the real vcruntime140_1.dll "
                           "could not be loaded\n");
        return FALSE;
    }
    load_preloader();
    return TRUE;
}
