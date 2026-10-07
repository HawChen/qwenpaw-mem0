# ============================================================
#  set_mem0_agent.ps1 — 为指定 Agent 开启/关闭火山引擎 Mem0
#
#  用法：
#    .\set_mem0_agent.ps1 -AgentId default                 # 开启（默认）
#    .\set_mem0_agent.ps1 -AgentId your_agent -Backend remelight   # 关闭（示例）
#    .\set_mem0_agent.ps1 -List                            # 列出各 Agent 当前设置
#
#  原理：修改 ~/.qwenpaw/workspaces/<AgentId>/agent.json 中的
#        running.memory_manager_backend 字段。
#        该字段按 Agent 独立生效，互不影响。
#
#  安全：写入前自动备份；写入后校验 JSON 合法性；只替换 1 处。
# ============================================================
param(
    [string]$AgentId,
    [ValidateSet("mem0", "remelight")]
    [string]$Backend = "mem0",
    [switch]$List
)

$WorkspacesRoot = Join-Path $HOME ".qwenpaw\workspaces"

function Get-BackendOf([string]$file) {
    $m = Select-String -Path $file -Pattern '"memory_manager_backend"\s*:\s*"([^"]*)"' |
         Select-Object -First 1
    if ($m -and $m.Matches.Count -gt 0) { return $m.Matches[0].Groups[1].Value }
    return "<未找到>"
}

# ---------- 列出模式 ----------
if ($List -or -not $AgentId) {
    Write-Host "=== 各 Agent 的记忆后端设置 ==="
    Get-ChildItem $WorkspacesRoot -Directory | ForEach-Object {
        $f = Join-Path $_.FullName "agent.json"
        if (Test-Path $f) {
            $b = Get-BackendOf $f
            $mark = if ($b -eq "mem0") { "  ← 火山引擎 Mem0（云端计费）" }
                    elseif ($b -eq "remelight") { "  ← ReMe（本地免费）" } else { "" }
            Write-Host ("  {0,-24} {1}{2}" -f $_.Name, $b, $mark)
        }
    }
    Write-Host ""
    Write-Host "用法: .\set_mem0_agent.ps1 -AgentId <agent_id> [-Backend mem0|remelight]"
    return
}

# ---------- 修改模式 ----------
$file = Join-Path $WorkspacesRoot "$AgentId\agent.json"
if (-not (Test-Path $file)) {
    Write-Host "错误：找不到 $file"
    exit 1
}

$current = Get-BackendOf $file
Write-Host "Agent      : $AgentId"
Write-Host "当前后端   : $current"

if ($current -eq $Backend) {
    Write-Host "已经是 '$Backend'，无需修改。"
    return
}

# 读取（保留原 BOM 状态）
$bytes  = [System.IO.File]::ReadAllBytes($file)
$hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
$text   = [System.IO.File]::ReadAllText($file, [System.Text.Encoding]::UTF8)

$pattern = '"memory_manager_backend"\s*:\s*"[^"]*"'
$count   = ([regex]::Matches($text, $pattern)).Count
if ($count -ne 1) {
    Write-Host "错误：预期 1 处匹配，实际 $count 处，已中止（避免误改）"
    exit 1
}

# 备份
$bakDir = Join-Path $HOME ".qwenpaw\workspaces\_backend_switch_backups"
New-Item -ItemType Directory -Path $bakDir -Force | Out-Null
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$bak = Join-Path $bakDir "$AgentId.$stamp.agent.json.bak"
Copy-Item $file $bak -Force
Write-Host "备份       : $bak"

# 替换 + 校验 + 写入
$updated = [regex]::Replace($text, $pattern, "`"memory_manager_backend`": `"$Backend`"")
try {
    $updated | ConvertFrom-Json | Out-Null
} catch {
    Write-Host "错误：替换后 JSON 非法，已中止：$($_.Exception.Message)"
    exit 1
}
[System.IO.File]::WriteAllText($file, $updated, (New-Object System.Text.UTF8Encoding($hasBom)))

Write-Host "已修改     : $current -> $Backend"
Write-Host ""
Write-Host "生效方式：重启 QwenPaw。"
if ($Backend -eq "mem0") {
    Write-Host "  云端 Mem0：插件会在启动后约 20 秒自动激活该 Agent。"
    Write-Host "  注意：自 2026-11-02 起按 Credit 计费，月度预算熔断默认 10 元。"
} else {
    Write-Host "  ReMe：本地免费，数据存于workspace 的 memory/ 目录。"
}