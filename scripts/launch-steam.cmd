@echo off
REM ===========================================================================
REM  Steam launch relay for sts2-mcp
REM
REM  Set this as the Steam launch option (right-click game > Properties):
REM
REM      "<repo>\scripts\launch-steam.cmd" %%command%%
REM
REM  Why not inline `cmd /C "set ... && %%command%%"`:
REM  Steam substitutes %%command%% with the QUOTED game exe path (it contains
REM  spaces). Nesting that inside an outer `cmd /C "..."` easily loses quotes,
REM  and it fails silently with no diagnostic. Relaying through this script
REM  avoids the quote nesting AND records launcher.log so we can tell whether
REM  it was invoked at all.
REM
REM  NOTE: this file must keep CRLF line endings; cmd.exe mis-parses LF files.
REM ===========================================================================

set "REPO=%~dp0.."
set "CORECLR_ENABLE_PROFILING=1"
set "CORECLR_PROFILER={27585C9F-BB81-4251-B62F-1B463AB4D58A}"
set "CORECLR_PROFILER_PATH_64=%REPO%\bin\Sts2Profiler.dll"
set "CORECLR_PROFILER_PATH=%REPO%\bin\Sts2Profiler.dll"

REM Repo root for the managed bridge. It is loaded from a temp copy and so
REM cannot derive this from its own assembly location - we already computed
REM the path above, so just pass it along.
set "STS2MCP_REPO=%REPO%"

REM Frame-loop attach. Required before stage 3: enqueueing game actions from the
REM HTTP thread would corrupt the action queue. Read-only /state tolerates the
REM degraded path, writes do not. Attach is attempted on the main thread first
REM (inside NGame..cctor), with a background-thread retry as fallback.
set "STS2MCP_ATTACH_FRAME=1"

REM Take over card selection (discard/search/"choose 1 of X") via the game's own
REM CardSelectCmd.UseSelector injection point, so choices are answered over MCP
REM instead of waiting for a mouse click. NOTE: this BYPASSES the selection UI -
REM with nothing answering, a choice would hang until the bridge's fallback
REM timeout. Clear this line to restore vanilla click-to-choose.
set "STS2MCP_CHOICE=1"

REM Optional overrides (leave unset for defaults):
REM   set "STS2MCP_PORT=8765"          bridge HTTP port

if not exist "%REPO%\logs" mkdir "%REPO%\logs"
echo [%DATE% %TIME%] launcher invoked>> "%REPO%\logs\launcher.log"
echo     profiler = %CORECLR_PROFILER_PATH_64%>> "%REPO%\logs\launcher.log"
echo     args     = %*>> "%REPO%\logs\launcher.log"

if not exist "%REPO%\bin\Sts2Profiler.dll" (
    echo     [WARN] profiler dll missing - game will start WITHOUT injection>> "%REPO%\logs\launcher.log"
)

REM Forward the full command line Steam passed in (game exe + its args)
%*
