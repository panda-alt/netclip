# netclip 暂存文件清理 —— 双击 clean.bat 调用，也可以直接右键"使用 PowerShell 运行"。
#
# 清的是什么
# ----------
#   %LOCALAPPDATA%\netclip\staging\<名字>\<传输ID>\<文件名>
#
# netclip 收到文件后会先落在那里，再按 `clipboard.files.staging_ttl_min`
# （默认 120 分钟）自己清理。这个脚本只是给你一个"想现在就清一下"的入口，
# 平时并不需要跑。
#
# **不碰 `Downloads\netclip`（或你配置的 receive_dir）。** 那是交付给你的文件，
# 不是暂存 —— 删了就真没了。
#
# 为什么删之前要看 netclip 在不在跑
# --------------------------------
#   * 正在传输的文件被删掉，这次传输会失败；
#   * `CF_HDROP` 只是**路径引用**。剪贴板里如果还放着暂存区的文件，
#     删掉之后粘贴就会变成"找不到文件"。
# 所以这里会先探一下，并且在有实例运行时把后果说清楚，而不是闷头删。

[CmdletBinding()]
param(
    # 覆盖暂存根目录。**测试用** —— 让测试可以拿一个临时目录跑完整流程，
    # 而不是去动真实的暂存区。
    [string]$Path,

    # 跳过确认直接删（自动化用；双击运行时不要加）。
    [switch]$Yes
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

function Write-Head($text) {
    Write-Host ''
    Write-Host ('=' * 62) -ForegroundColor DarkGray
    Write-Host "  $text" -ForegroundColor Cyan
    Write-Host ('=' * 62) -ForegroundColor DarkGray
}

function Write-Info($text) { Write-Host "  $text" -ForegroundColor Gray }
function Write-Ok($text)   { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Write-Warn2($text) { Write-Host "  [警告] $text" -ForegroundColor Yellow }
function Write-Err($text)  { Write-Host "  [错误] $text" -ForegroundColor Red }

function Format-Size($bytes) {
    $value = 0.0
    if ($null -ne $bytes) { $value = [double]$bytes }
    if ($value -ge 1GB) { return ('{0:N2} GB' -f ($value / 1GB)) }
    if ($value -ge 1MB) { return ('{0:N2} MB' -f ($value / 1MB)) }
    if ($value -ge 1KB) { return ('{0:N1} KB' -f ($value / 1KB)) }
    return ('{0} 字节' -f [int]$value)
}

function Measure-Tree($path) {
    #> 返回 @{ Files = 文件数; Bytes = 总字节 }；路径不存在就是全 0。 #>
    if (-not (Test-Path -LiteralPath $path)) {
        return @{ Files = 0; Bytes = 0 }
    }
    $files = @(Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $sum = ($files | Measure-Object -Property Length -Sum).Sum
    if ($null -eq $sum) { $sum = 0 }
    return @{ Files = $files.Count; Bytes = $sum }
}

function Get-NetclipListener {
    <#
        返回正在监听 netclip 端口的进程 ID，没有就返回 0。

        **靠端口而不是靠进程名/命令行。** netclip 可能是以管理员身份运行的，
        那时普通权限的脚本读它的 CommandLine 会得到空串。端口是它自己约定的
        （input=listen_port，clip/file 各 +1/+2，见 start.ps1），跨权限也能查到。
        这个默认值要和 config.toml 里的 network.listen_port 一致。
    #>
    foreach ($port in 24800, 24801, 24802) {
        $conn = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
        if ($conn.Count -gt 0) { return [int]$conn[0].OwningProcess }
    }
    return 0
}

Write-Head 'netclip 暂存文件清理'

$stagingRoot = if ($Path) { $Path } else { Join-Path $env:LOCALAPPDATA 'netclip\staging' }
Write-Info "暂存根目录: $stagingRoot"
Write-Host ''

if (-not (Test-Path -LiteralPath $stagingRoot)) {
    Write-Ok '没有需要清理的暂存文件（目录不存在）。'
    exit 0
}

$total = Measure-Tree $stagingRoot
if ($total.Files -eq 0) {
    Write-Ok '没有需要清理的暂存文件（目录是空的）。'
    Write-Info '注意：这里只清暂存区。`Downloads\netclip` 是交付目录，不归它管。'
    exit 0
}

Write-Info ('将要删除: {0} 个文件，合计 {1}' -f $total.Files, (Format-Size $total.Bytes))
Write-Host ''
foreach ($peer in @(Get-ChildItem -LiteralPath $stagingRoot -Directory -ErrorAction SilentlyContinue)) {
    $sub = Measure-Tree $peer.FullName
    $transfers = @(Get-ChildItem -LiteralPath $peer.FullName -Directory -ErrorAction SilentlyContinue)
    Write-Info ('  {0,-24} {1,3} 次传输  {2,3} 个文件  {3}' -f `
        $peer.Name, $transfers.Count, $sub.Files, (Format-Size $sub.Bytes))
}
Write-Host ''

$listener = Get-NetclipListener
if ($listener -ne 0) {
    Write-Warn2 "netclip 正在运行（PID $listener）。"
    Write-Info '  * 正在传输的文件被删掉，这次传输会失败；'
    Write-Info '  * 剪贴板里如果还放着暂存区的文件，删完粘贴会变成"找不到文件"。'
    Write-Info '  想稳妥就先停掉 netclip（start.bat 选第 3 项），再运行这个脚本。'
    Write-Host ''
}

$answer = if ($Yes) { 'y' } else { Read-Host '  确定要删除吗？(y/N)' }
if ($answer -notmatch '^[yY]') {
    Write-Info '已取消，什么都没删。'
    exit 0
}

Write-Host ''
Remove-Item -LiteralPath $stagingRoot -Recurse -Force -ErrorAction SilentlyContinue

$left = Measure-Tree $stagingRoot
$freed = $total.Bytes - $left.Bytes
if ($freed -lt 0) { $freed = 0 }

if ($left.Files -eq 0) {
    Write-Ok ('已清空，释放 {0}。' -f (Format-Size $freed))
    Write-Info '交付目录（Downloads\netclip）没有被动过。'
    exit 0
}

Write-Warn2 ('删掉了 {0}，还有 {1} 个文件没能删除。' -f (Format-Size $freed), $left.Files)
Write-Info '剩下的多半正被 netclip 占用。先停掉它（start.bat 选第 3 项）再运行一次。'
exit 1
