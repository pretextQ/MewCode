# Keep terminal cleanup in the parent shell, even if the TUI child is terminated.
$ErrorActionPreference = 'Stop'

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
$projectPath = Split-Path -Parent $PSScriptRoot

Push-Location -LiteralPath $projectPath
try {
    $env:NO_COLOR = $null
    $env:TERM = 'xterm-256color'
    & uv run mewcode
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
