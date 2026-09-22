# netclip 启动器（由 start.bat 调用，也可以直接右键"使用 PowerShell 运行"）
#
# 为什么逻辑放在 PowerShell 里而不是直接写在 .bat 里：
#   * .bat 文件是任意编码的字节流，cmd.exe 按系统 ANSI 代码页（中文机器上是
#     GBK/936）逐行解释它。用 UTF-8 保存的中文会变成乱码，用 GBK 保存又会在
#     别的区域设置下乱码 —— 无解。所以 .bat 只做一件事：用 -File 把控制权交给
#     这个脚本，自己一行中文都不输出。
#   * PowerShell 里能方便地做"找 Python → 检查配置 → 装依赖 → 选启动方式"。

[CmdletBinding()]
param(
    # 非交互启动：给「任务计划程序」和 netclip_task.vbs 用。
    # 不打印菜单、不 Read-Host（隐藏窗口下 Read-Host 会让任务永远挂住），
    # 停掉旧实例后直接后台拉起，用退出码报告结果。
    [switch]$Run
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $Root

# 让 Python 的输出按 UTF-8 解释，否则中文日志在 GBK 控制台里是乱码
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

function Test-Elevated {
    <#
        本启动器是否以管理员身份运行。

        **为什么值得单独判一次**：Windows 的 UIPI（用户界面特权隔离）规定，未提权
        进程的低级钩子（WH_MOUSE_LL / WH_KEYBOARD_LL）和 SendInput 对**已提权窗口**
        （任务管理器、注册表编辑器、安装程序……）一律无效。现象是鼠标一移到那个窗口
        上就"卡住" —— 其实是钩子被系统静默屏蔽，netclip 连事件都收不到。

        这是系统限制而非程序缺陷：Deskflow 有同样的 issue（#8611），QQ 远程桌面也一样。
        唯一的解是让 netclip 自己提权运行 —— 高完整性 -> 低完整性是允许的。
    #>
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal $identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# ---------------------------------------------------------------- 找 Python

function Find-Python {
    <#
        优先用 python 前缀的启动器（会自己挑一个已注册的解释器），
        找不到就退回 PATH 上的 python.exe。**不要**用 pythonw.exe ——
        这里需要控制台输出。
    #>
    $candidates = @(
        @{ Cmd = 'py';    Args = @('-3') },
        @{ Cmd = 'python'; Args = @() },
        @{ Cmd = 'python3'; Args = @() }
    )
    foreach ($c in $candidates) {
        $exe = Get-Command $c.Cmd -ErrorAction SilentlyContinue
        if ($null -eq $exe) { continue }
        try {
            $probe = & $exe.Source @($c.Args + @('-c', 'import sys;print(sys.version_info[0])')) 2>$null
        } catch { continue }
        if ("$probe".Trim() -eq '3') {
            return @{ Exe = $exe.Source; Prefix = $c.Args }
        }
    }
    return $null
}

$py = Find-Python
if ($null -eq $py) {
    Write-Err '没有找到 Python 3。'
    Write-Info '请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/'
    Write-Info '安装时记得勾选 "Add python.exe to PATH"。'
    exit 1
}

$python = $py.Exe
$prefix = $py.Prefix

function Invoke-Netclip {
    param([string[]] $ExtraArgs)
    <#
        用 & 调用而不是 Start-Process，输出直接进当前控制台。

        **必须把输出管道到 Out-Host**：PowerShell 函数的返回值是它所有未消费的
        输出。如果写成 `& $python ...; return $LASTEXITCODE`，子进程的 stdout
        会被算进返回值里 —— 最后拿到的"退出码"是一大段日志再加一个数字。
        这是 PowerShell 里非常容易踩的坑。
    #>
    & $python @prefix @ExtraArgs | Out-Host
    return $LASTEXITCODE
}

function New-ArgList {
    <# 拼出 "python -m netclip ..." 的完整参数数组（含 py 启动器的 -3） #>
    param([string[]] $Rest)
    $list = @()
    $list += $prefix
    $list += @('-m', 'netclip')
    $list += $Rest
    return $list
}

# `-Run`（计划任务用）不打印这些：输出进了隐藏窗口，纯属浪费，
# 而且手动执行 `start.ps1 -Run` 时看到半截菜单会让人以为还得选数字。
if (-not $Run) {
    Write-Head 'netclip 启动器'
    Write-Info "Python : $python"
    Write-Info "工作目录: $Root"
}

$isElevated = Test-Elevated
if (-not $Run) {
    if ($isElevated) {
        Write-Ok '权限   : 管理员 —— 可以把鼠标键盘送进任务管理器等提权窗口'
    } else {
        Write-Warn2 '权限   : 普通用户 —— 任务管理器等提权窗口收不到转发的鼠标键盘'
        Write-Info '         (Windows UIPI 系统限制，Deskflow/QQ 远程桌面同样如此。选第 10 项可提权)'
    }
}

# ---------------------------------------------------------------- 检查配置

$configPath = Join-Path $Root 'config.toml'
$examplePath = Join-Path $Root 'config.example.toml'

if (-not (Test-Path -LiteralPath $configPath)) {
    Write-Warn2 '还没有 config.toml。'
    if (Test-Path -LiteralPath $examplePath) {
        Copy-Item -LiteralPath $examplePath -Destination $configPath
        Write-Ok '已从 config.example.toml 生成 config.toml'
    } else {
        Write-Err 'config.example.toml 也不在，无法生成配置。'
        exit 1
    }
    Write-Host ''
    Write-Warn2 '首次使用必须改这三处（两台机器都要改）：'
    Write-Info '  1. network.peer_ip      填对方的 IP'
    Write-Info '  2. [layout] peer_position 对端在本机哪一侧（right/left/up/down）'
    Write-Info '  3. security.psk         两台必须完全一致'
    if ($Run) {
        # 计划任务里窗口是隐藏的，Read-Host 会让任务永远挂住 —— 直接失败退出。
        Write-Err 'config.toml 还没配置好，非交互模式直接退出。'
        exit 1
    }
    Write-Host ''
    $open = Read-Host '  现在打开 config.toml 编辑吗？(Y/n)'
    if ($open -notmatch '^[nN]') {
        Start-Process notepad.exe -ArgumentList "`"$configPath`""
        Write-Info '改完保存后，重新双击 start.bat。'
    }
    exit 0
}

# ---------------------------------------------------------------- 检查依赖

# netclip 只用标准库 + 自带的 ctypes 绑定，所以这里只是打印版本，不是硬检查。
#
# 注意这里用 chr(0x2E) 拼出小数点，而**不要**在单引号字符串里写双引号：
# PowerShell 把参数传给原生 exe 时会剥掉内层双引号（Windows 命令行没有转义机制），
# Python 收到的就变成 `print(..join(...))`，报一个看起来毫不相干的 SyntaxError。
$probe = & $python @prefix @(
    '-c', 'import sys; print(chr(0x2E).join(map(str, sys.version_info[:2])))'
) | Select-Object -Last 1
if (-not $Run) { Write-Info "版本   : Python $probe" }

# ---------------------------------------------------------------- 选择动作

if (-not $Run) {
    Write-Host ''
    Write-Info '请选择要做什么：'
    Write-Host ''
    Write-Host '  1) 启动 netclip（后台运行，用右下角托盘图标控制）' -ForegroundColor White
    Write-Host '  2) 启动并显示日志窗口（排查问题用）' -ForegroundColor White
    Write-Host '  3) 停止正在运行的 netclip' -ForegroundColor White
    Write-Host '  4) 重启 netclip（改了配置或换了代码之后用这个）' -ForegroundColor White
    Write-Host '  5) 只校验配置，不启动' -ForegroundColor White
    Write-Host '  6) 运行自检（连通性 / 剪贴板 / 文件 / 托盘）' -ForegroundColor White
    Write-Host '  7) 打开 config.toml' -ForegroundColor White
    Write-Host '  8) 打开日志文件' -ForegroundColor White
    Write-Host '  9) 开机自动启动：安装 / 卸载' -ForegroundColor White
    Write-Host ' 10) 以管理员身份重启本启动器（解决任务管理器等提权窗口卡住）' -ForegroundColor White
    Write-Host '  0) 退出' -ForegroundColor White
    Write-Host ''
}

function Get-NetclipProcesses {
    <#
        找出正在运行的 netclip 进程。

        判据是**命令行里含 "-m netclip"** 加进程路径是本机 Python。
        不能只看进程名（python 可能是你自己的脚本），也不能只看路径
        （同一台机器上可能有好几个 python 程序）。

        **还要兜一层端口。** netclip 如果是**以管理员身份**运行的，普通权限的
        启动器读它的 CommandLine 会得到空串，上面那个判据就永远匹配不上 ——
        于是"停止/重启"报告"当前没有正在运行的 netclip"，可它明明就在那儿，
        接着启动新实例又会撞端口。24800/24801/24802 是我们自己约定的监听端口，
        拿它反查 PID 不会认错，而且**跨权限级别也能查到**。
    #>
    $found = [ordered]@{}
    foreach ($name in @('python', 'pythonw')) {
        foreach ($proc in (Get-CimInstance Win32_Process -Filter "Name='$name.exe'" -ErrorAction SilentlyContinue)) {
            $cmd = "$($proc.CommandLine)"
            if ($cmd -match '(?i)-m\s+netclip(\s|$|")') {
                $procId = [int]$proc.ProcessId
                if (-not $found.Contains($procId)) {
                    $found.Add($procId, [pscustomobject]@{
                        Id      = $procId
                        Name    = $name
                        Command = $cmd
                        How     = '命令行'
                    })
                }
            }
        }
    }
    # 注意两件事：
    #   * `$pid` 是 PowerShell 的自动变量（当前进程 ID），绝不能覆盖它；
    #   * `[ordered]@{}` 是 OrderedDictionary，**不能用 `$d[$k] = $v` 新增键**
    #     （它会抛 ArgumentOutOfRangeException，参数名 index），只能 `.Add()`。
    #     普通 `@{}` 才允许那样写 —— 这里踩过一次。
    foreach ($port in 24800, 24801, 24802) {
        foreach ($conn in (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)) {
            $ownerId = [int]$conn.OwningProcess
            if (-not $found.Contains($ownerId)) {
                $found.Add($ownerId, [pscustomobject]@{
                    Id      = $ownerId
                    Name    = '(读不到)'
                    Command = '<读不到命令行 —— 多半是管理员权限运行的>'
                    How     = "监听端口 $port"
                })
            }
        }
    }
    #: `return ,@(...)` 里那个**一元逗号不能省**。PowerShell 的 `return` 会把集合
    #: **展开**：只有一个元素时调用方拿到的是单个对象而不是数组，于是脚本开头的
    #: `Set-StrictMode -Version Latest` 会让 `$procs.Count` 直接报
    #: "The property 'Count' cannot be found on this object"。真机上就是这么炸的。
    #: 一元逗号包一层，保证调用方永远拿到数组。
    return ,@($found.Values)
}

function Stop-NetclipProcesses {
    $procs = Get-NetclipProcesses
    if (-not $procs) {
        Write-Info '当前没有正在运行的 netclip。'
        return $false
    }
    foreach ($p in $procs) {
        Write-Info "停止 PID $($p.Id)（由$($p.How)找到）..."
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 700
    $left = Get-NetclipProcesses
    if ($left) {
        Write-Warn2 "还有 $($left.Count) 个进程没停掉。"
        if ($isElevated) {
            Write-Info '请在任务管理器里结束它，然后重试。'
        } else {
            # 普通权限的进程**杀不掉**管理员权限的进程，这不是"没找对 PID"。
            # 之前这里只报一句"可能需要在任务管理器里结束"，看不出是权限问题。
            Write-Err '本启动器是普通权限，无法停止以管理员身份运行的 netclip。'
            Write-Info '请先用第 10 项以管理员身份重开启动器，再执行停止 / 重启。'
        }
        return $false
    }
    Write-Ok '已停止。'
    return $true
}

function Start-NetclipBackground {
    param(
        # 非交互模式：所有 Read-Host 一律跳过。隐藏窗口下 Read-Host 会**永远挂住**，
        # 计划任务看起来就是"启动了但什么都没发生"。宁可失败退出，也不要卡死。
        [switch]$NoPrompt
    )
    # 后台运行：pythonw.exe 没有控制台窗口，程序会自己退到托盘。
    # 日志仍然写到 netclip.log，托盘右键可以"打开日志"。
    $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }

    # 先检查端口是否被占：多半是上一个实例还没退干净。
    # 不做这一步的话，新实例会因为 24800 被占而静默起不来，用户只看到"没反应"。
    $busy = Get-NetTCPConnection -LocalPort 24800 -State Listen -ErrorAction SilentlyContinue
    if ($busy) {
        Write-Warn2 "端口 24800 已被占用（PID $($busy[0].OwningProcess)）—— 可能已有一个实例在跑。"
        if ($NoPrompt) {
            Write-Err '端口被占用，非交互模式不自动处理，放弃启动。'
            return $false
        }
        $again = Read-Host '  要先把旧的停掉再启动吗？(Y/n)'
        if ($again -notmatch '^[nN]') {
            if (-not (Stop-NetclipProcesses)) { return $false }
            Start-Sleep -Milliseconds 500
        } else {
            Write-Warn2 '已取消。'
            return $false
        }
    }

    Write-Ok '正在后台启动 netclip……'
    # 别把这个数组叫 $args —— PowerShell 的 $args 是自动变量（存未绑定参数），
    # 覆盖它容易让人读代码时误解，之前也确实在这上面绕过一次弯路。
    $argList = (New-ArgList @())
    Start-Process -FilePath $pythonw -ArgumentList $argList -WorkingDirectory $Root | Out-Null
    Start-Sleep -Seconds 2

    $proc = Get-NetclipProcesses
    if ($proc) {
        Write-Ok "netclip 已在运行（PID $($proc[0].Id)）"
        Write-Info '右下角托盘里找一个圆角方块图标，右键有菜单。'
        Write-Info "日志：$(Join-Path $Root 'netclip.log')"
        return $true
    }
    Write-Warn2 '没有检测到进程，可能启动就失败了。'
    Write-Info '请改用"启动并显示日志窗口"看具体报错。'
    return $false
}

# ---------------------------------------------------------------- 非交互启动
#
# `start.ps1 -Run`：给「任务计划程序」和 netclip_task.vbs 用。
# 不打印菜单、不问任何问题，停掉旧实例后直接后台拉起，用退出码报告结果。
#
# 放在这里（所有函数定义之后）是因为 PowerShell 按顺序执行语句 ——
# 函数必须先被执行过才能调用，所以这段不能提前。
if ($Run) {
    $existing = Get-NetclipProcesses
    if ($existing) {
        Write-Info "已有 netclip 在运行（PID $($existing[0].Id)），先停掉再启动。"
        if (-not (Stop-NetclipProcesses)) {
            Write-Err '停不掉旧实例，放弃启动。'
            exit 1
        }
        Start-Sleep -Milliseconds 500
    }
    if (Start-NetclipBackground -NoPrompt) { exit 0 }
    exit 1
}

$choice = Read-Host '  输入数字后回车'
switch ("$choice".Trim()) {

    '1' {
        Write-Host ''
        [void](Start-NetclipBackground)
    }

    '2' {
        Write-Host ''
        Write-Info '窗口模式：日志会实时打在下面。Ctrl+C 退出。'
        Write-Info '提示：如果只是日常使用，建议用第 1 项后台运行。'
        $running = Get-NetclipProcesses
        if ($running) {
            Write-Warn2 "已有 netclip 在运行（PID $($running[0].Id)），端口会冲突。"
            $stopIt = Read-Host '  先停掉它吗？(Y/n)'
            if ($stopIt -notmatch '^[nN]') { [void](Stop-NetclipProcesses) }
        }
        Write-Host ''
        $code = Invoke-Netclip (New-ArgList @('--no-tray'))
        Write-Host ''
        if ($code -ne 0) { Write-Warn2 "netclip 退出码 $code" } else { Write-Ok 'netclip 已正常退出' }
    }

    '3' {
        Write-Host ''
        [void](Stop-NetclipProcesses)
    }

    '4' {
        Write-Host ''
        Write-Info '重启：先停掉旧进程，再用新代码/新配置启动。'
        [void](Stop-NetclipProcesses)
        Start-Sleep -Milliseconds 500
        Write-Host ''
        [void](Start-NetclipBackground)
    }

    '5' {
        Write-Host ''
        $code = Invoke-Netclip (New-ArgList @('--check'))
        Write-Host ''
        if ($code -eq 0) { Write-Ok '配置没问题' } else { Write-Err "配置有问题（退出码 $code）" }
    }

    '6' {
        Write-Host ''
        Write-Info '请选择自检项：'
        Write-Host '  1) 环境与依赖        (env)'
        Write-Host '  2) 剪贴板读写往返    (clipboard)   ← 会临时改写剪贴板，结束后还原'
        Write-Host '  3) 剪贴板同步环回    (clip-loop)'
        Write-Host '  4) 文件同步环回      (files-loop)'
        Write-Host '  5) 托盘图标          (tray)'
        Write-Host '  6) 全局钩子          (hooks)  ← 需要你在测试期间动鼠标敲键盘'
        Write-Host '  7) 按键诊断          (keys)   ← 打印 vk/扫描码，排查"某个键不对"'
        Write-Host '  8) 连通性            (loop)   ← 连上对端收发帧，但**不转发**输入（最安全）'
        Write-Host '  9) 回声防护          (warpguard) ← 查"鼠标被吸住高频抖动、推不动"'
        Write-Host ' 10) 注入识别          (inject)    ← 查"自己的注入被当成真实输入"自伤死循环'
        Write-Host ''
        $sub = Read-Host '  输入数字后回车'
        $map = @{
            '1' = @('env');        '2' = @('clipboard'); '3' = @('clip-loop');
            '4' = @('files-loop'); '5' = @('tray');      '6' = @('hooks', '--seconds', '5');
            '7' = @('keys', '--seconds', '8');
            '8' = @('loop', '--seconds', '10');
            '9' = @('warpguard');  '10' = @('inject')
        }
        $key = "$sub".Trim()
        if (-not $map.ContainsKey($key)) { Write-Warn2 '没选，退出。'; exit 0 }
        Write-Host ''
        $code = Invoke-Netclip (@('-m', 'netclip.selftest') + $map[$key])
        Write-Host ''
        if ($code -eq 0) { Write-Ok '自检通过' } else { Write-Err "自检未通过（退出码 $code）" }
    }

    '7' {
        Start-Process notepad.exe -ArgumentList "`"$configPath`""
        Write-Info '已打开 config.toml'
    }

    '8' {
        $logPath = Join-Path $Root 'netclip.log'
        if (Test-Path -LiteralPath $logPath) {
            Start-Process notepad.exe -ArgumentList "`"$logPath`""
            Write-Info "已打开 $logPath"
        } else {
            Write-Warn2 '还没有日志文件（说明还没用日志模式跑过）。'
            Write-Info '先用第 2 项启动一次，就会生成 netclip.log。'
        }
    }

    '9' {
        Write-Host ''
        Write-Info '用 Windows 计划任务实现开机自启（登录时启动）。'
        Write-Host '  1) 安装自启'
        Write-Host '  2) 卸载自启'
        Write-Host '  3) 查看当前状态'
        Write-Host ''
        $sub = Read-Host '  输入数字后回车'
        $taskName = 'netclip'

        switch ("$sub".Trim()) {
            '1' {
                Write-Host ''
                Write-Info '以"最高权限"注册计划任务的话，登录时由系统直接提权启动，不弹 UAC，'
                Write-Info '而且能控制任务管理器这类提权窗口。代价是注册这一步本身需要管理员。'
                $answer = Read-Host '  以最高权限运行？(Y/n)'
                $runLevel = if ($answer -match '^[nN]') { 'Limited' } else { 'Highest' }
                if ($runLevel -eq 'Highest' -and -not $isElevated) {
                    Write-Err '注册"最高权限"任务需要管理员。请先用第 10 项提权重开启动器。'
                    Write-Info '想现在就用普通权限装，重新选一次并回答 n。'
                    break
                }

                $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
                if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }
                $action = New-ScheduledTaskAction -Execute $pythonw -Argument '-m netclip' -WorkingDirectory $Root
                $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
                $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -StartWhenAvailable `
                    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3 `
                    -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
                try {
                    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
                        -Settings $settings -RunLevel $runLevel `
                        -Description 'netclip: 跨机鼠标键盘共享与剪贴板同步' -Force | Out-Null
                    Write-Ok "已安装自启：$taskName（权限：$runLevel）"
                    Write-Info '下次登录时会自动启动。现在想启动就用第 1 项。'
                } catch {
                    Write-Err "安装失败：$($_.Exception.Message)"
                    Write-Info '可以先用第 10 项以管理员身份重开启动器，再回来装。'
                }
            }
            '2' {
                try {
                    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop
                    Write-Ok "已卸载自启：$taskName"
                } catch {
                    Write-Warn2 "没有找到名为 $taskName 的计划任务（可能本来就没装）。"
                }
            }
            '3' {
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                if ($null -eq $task) {
                    Write-Info '当前没有安装自启。'
                } else {
                    Write-Ok "已安装：状态 = $($task.State)  权限 = $($task.Principal.RunLevel)"
                    ($task.Actions | ForEach-Object { "    $($_.Execute) $($_.Arguments)" }) | Write-Info
                }
            }
            default { Write-Warn2 '没选，退出。' }
        }
    }

    '10' {
        Write-Host ''
        if ($isElevated) {
            Write-Ok '当前已经是管理员，不需要重启。'
        } else {
            Write-Info '将以管理员身份重新打开本启动器（会弹一次 UAC 确认）。'
            Write-Info '提权后 netclip 才能把鼠标/键盘送进任务管理器这类提权窗口。'
            Write-Info '新窗口里选第 4 项启动，它会自动把现在这个普通权限的实例停掉。'
            Write-Host ''
            try {
                $bat = Join-Path $Root 'start.bat'
                Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$bat`"" -Verb RunAs | Out-Null
                Write-Ok '已请求提权。'
            } catch {
                Write-Err "提权失败或被取消：$($_.Exception.Message)"
            }
        }
    }

    '0' { exit 0 }
    default { Write-Warn2 '没选，退出。' }
}

Write-Host ''
