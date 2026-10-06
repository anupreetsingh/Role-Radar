/* Role Radar Checker.exe: runs the Python beside it (python313.dll) as python.exe would, on the arguments
 * given: "-m role_radar start ..." (the checker) and each role-radar command the app runs. So every
 * Role Radar process is named Role Radar in Task Manager, with its icon, never "Python", and none opens
 * a console window. (It's a GUI program: a command's output still reaches the app, through a pipe.)
 *
 * Python runs in UTF-8 mode (-X utf8), and python313._pth beside it isolates it: only Role Radar's own
 * folder and packages are on its path, whatever Python-related settings this PC has.
 * Built by scripts/package_windows.sh, with zig (x86_64-windows-gnu). */

#include <windows.h>
#include <shellapi.h>
#include <stdlib.h>
#include <wchar.h>

typedef int (*PyMain)(int argc, wchar_t **argv);

static int fail(const wchar_t *message) {
    MessageBoxW(NULL, message, L"Role Radar", MB_OK | MB_ICONERROR);
    return 1;
}

int WINAPI wWinMain(HINSTANCE instance, HINSTANCE previous, PWSTR line, int show) {
    (void)instance, (void)previous, (void)line, (void)show;
    int count = 0;
    wchar_t **given = CommandLineToArgvW(GetCommandLineW(), &count);
    wchar_t python[MAX_PATH + 16];
    DWORD length = GetModuleFileNameW(NULL, python, MAX_PATH);
    wchar_t *slash = length && length < MAX_PATH ? wcsrchr(python, L'\\') : NULL;
    if (!given || !slash) {
        return fail(L"Role Radar couldn't find its own folder. Reinstall Role Radar.");
    }
    wcscpy(slash + 1, L"python313.dll");
    HMODULE library = LoadLibraryExW(python, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    PyMain py_main = library ? (PyMain)GetProcAddress(library, "Py_Main") : NULL;
    if (!py_main) {
        return fail(L"Role Radar couldn't start its Python (python313.dll). Reinstall Role Radar.");
    }

    /* Python's arguments: UTF-8 mode, then those given. */
    wchar_t **args = calloc((size_t)count + 3, sizeof *args);
    if (!args) {
        return fail(L"Role Radar ran out of memory starting.");
    }
    int n = 0;
    args[n++] = given[0];
    args[n++] = L"-X";
    args[n++] = L"utf8";
    for (int i = 1; i < count; i++) {
        args[n++] = given[i];
    }
    return py_main(n, args);
}
