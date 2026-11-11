param(
    [Parameter(Mandatory=$true)][AllowEmptyString()][string]$BaseUrl,
    [Parameter(Mandatory=$true)][string]$ModelName,
    [ValidateRange(1,65535)][int]$Port=8092,
    [Security.SecureString]$ApiKey,
    [switch]$Reload
)
$ErrorActionPreference='Stop'
# 先检查请求地址。相对 URI 没有 AbsoluteUri，错误提示也不能回显误填的密钥。
$modelUri=$null
if (-not [uri]::TryCreate($BaseUrl.Trim(), [UriKind]::Absolute, [ref]$modelUri) -or
    $modelUri.Scheme -notin @('https','http') -or
    [string]::IsNullOrWhiteSpace($modelUri.Host) -or
    $modelUri.UserInfo) {
    throw 'Base URL 必须是以 https:// 或 http:// 开头的模型服务地址。百炼请复制业务空间的 API Host，使用 /compatible-mode/v1 路径。API Key 请在脚本提示时隐藏输入。'
}
# 密钥仅保存在当前进程及子进程环境，不写入项目文件或命令行参数。
$credentialValue=$ApiKey
$secretPointer=[IntPtr]::Zero
$previousKey=$env:MODEL_API_KEY
$previousUrl=$env:MODEL_BASE_URL
$previousModel=$env:MODEL_NAME
try {
    # 终端取消或不支持隐藏输入时可能返回 null，检查后才允许转换。
    if ($null -ne $credentialValue -and $credentialValue.Length -eq 0) {
        throw 'API Key 为空，请重新输入。'
    }
    if ($null -eq $credentialValue -and [string]::IsNullOrWhiteSpace($previousKey)) {
        $credentialValue=Read-Host -Prompt '模型 API Key（输入隐藏，粘贴后按回车）' -AsSecureString
        if ($null -eq $credentialValue -or $credentialValue.Length -eq 0) {
            throw '未读取到 API Key，可能取消了输入或当前终端不支持隐藏输入。请在本机 PowerShell 中重试；也可使用 -ApiKey 参数传入 SecureString。'
        }
    }
    if ($null -ne $credentialValue) {
        $secretPointer=[Runtime.InteropServices.Marshal]::SecureStringToBSTR($credentialValue)
        $env:MODEL_API_KEY=[Runtime.InteropServices.Marshal]::PtrToStringBSTR($secretPointer)
    }
    if ([string]::IsNullOrWhiteSpace($env:MODEL_API_KEY)) { throw 'API Key 为空，服务未启动。' }
    $env:MODEL_BASE_URL=$modelUri.AbsoluteUri.TrimEnd('/')
    $env:MODEL_NAME=$ModelName
    Write-Host "正在启动模型服务：$ModelName，端口 $Port；API Key 不会显示或保存。"
    & (Join-Path $PSScriptRoot 'dev.ps1') -Task run -Port $Port -Reload:$Reload
} finally {
    if ($secretPointer -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secretPointer) }
    $env:MODEL_API_KEY=$previousKey
    $env:MODEL_BASE_URL=$previousUrl
    $env:MODEL_NAME=$previousModel
}
