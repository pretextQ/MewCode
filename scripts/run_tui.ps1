# Keep terminal cleanup in the parent shell, even if the TUI child is terminated.
param(
    [Parameter(Position = 0)]
    [string]$WorkDir,
    [string]$Config
)

$ErrorActionPreference = 'Stop'

$projectPath = Split-Path -Parent $PSScriptRoot
$hasWorkDir = -not [string]::IsNullOrWhiteSpace($WorkDir)
if (-not $hasWorkDir) { $WorkDir = $projectPath }
$workPath = (Resolve-Path -LiteralPath $WorkDir).ProviderPath
if (-not (Test-Path -LiteralPath $workPath -PathType Container)) {
    throw "WorkDir must be a directory: $WorkDir"
}
if (-not [string]::IsNullOrWhiteSpace($Config)) {
    $Config = (Resolve-Path -LiteralPath $Config).ProviderPath
}
elseif ($hasWorkDir) {
    # Reuse this checkout's model config when opening another directory.
    $Config = Join-Path $projectPath '.mewcode/config.yaml'
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        # Let normal user/project discovery handle installations without this file.
        $Config = $null
    }
}

if (-not ('MewCode.TerminalModes' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace MewCode {
    public static class TerminalModes {
        [DllImport("kernel32.dll")] public static extern IntPtr GetStdHandle(int id);
        [DllImport("kernel32.dll")] public static extern bool GetConsoleMode(IntPtr h, out uint mode);
        [DllImport("kernel32.dll")] public static extern bool SetConsoleMode(IntPtr h, uint mode);
    }
}
'@
}

$inputHandle = [MewCode.TerminalModes]::GetStdHandle(-10)
$outputHandle = [MewCode.TerminalModes]::GetStdHandle(-11)
[uint32]$inputMode = 0
[uint32]$outputMode = 0
$hasInputMode = [MewCode.TerminalModes]::GetConsoleMode($inputHandle, [ref]$inputMode)
$hasOutputMode = [MewCode.TerminalModes]::GetConsoleMode($outputHandle, [ref]$outputMode)
$previousNoColor = $env:NO_COLOR
$previousTerm = $env:TERM

Push-Location -LiteralPath $workPath
try {
    $env:NO_COLOR = $null
    $env:TERM = 'xterm-256color'
    $launchArgs = @('run', '--project', $projectPath, 'mewcode')
    if (-not [string]::IsNullOrWhiteSpace($Config)) { $launchArgs += @('--config', $Config) }
    & uv @launchArgs
}
finally {
    try {
        if ($hasOutputMode) {
            [void][MewCode.TerminalModes]::SetConsoleMode($outputHandle, ($outputMode -bor 4))
        }
        $escape = [char]27
        # Disable all supported mouse encodings, focus/paste and Kitty keyboard mode.
        [Console]::Out.Write(
            "${escape}[?1000l${escape}[?1002l${escape}[?1003l${escape}[?1005l" +
            "${escape}[?1006l${escape}[?1015l${escape}[?1004l${escape}[?2004l" +
            "${escape}[<u${escape}[?25h${escape}[0m"
        )
        [Console]::Out.Flush()
    }
    finally {
        if ($hasInputMode) { [void][MewCode.TerminalModes]::SetConsoleMode($inputHandle, $inputMode) }
        if ($hasOutputMode) { [void][MewCode.TerminalModes]::SetConsoleMode($outputHandle, $outputMode) }
        $env:NO_COLOR = $previousNoColor
        $env:TERM = $previousTerm
        Pop-Location
    }
}
