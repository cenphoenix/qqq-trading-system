param(
    [Parameter(Mandatory = $true)]
    [string]$ExportDir,

    [Parameter(Mandatory = $true)]
    [string]$UserName,

    [Parameter(Mandatory = $true)]
    [string]$OutputDir
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Web

function Get-MessagePageNumber {
    param([System.IO.FileInfo]$File)

    if ($File.Name -eq 'messages.html') {
        return 1
    }

    if ($File.Name -match '^messages(\d+)\.html$') {
        return [int]$Matches[1]
    }

    return [int]::MaxValue
}

function Convert-HtmlFragmentToText {
    param([string]$Html)

    $text = $Html -replace '(?i)<br\s*/?>', "`n"
    $text = $text -replace '(?is)<script.*?</script>', ''
    $text = $text -replace '(?is)<style.*?</style>', ''
    $text = $text -replace '(?is)<[^>]+>', ''
    $text = [System.Web.HttpUtility]::HtmlDecode($text)
    $text = $text -replace "`r", ''
    $text = $text -replace "[`t ]+\n", "`n"
    $text = $text -replace "\n{3,}", "`n`n"
    return $text.Trim()
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$files = Get-ChildItem -LiteralPath $ExportDir -Filter 'messages*.html' |
    Sort-Object @{ Expression = { Get-MessagePageNumber $_ } }

$rows = New-Object System.Collections.Generic.List[object]
$lastFrom = $null

foreach ($file in $files) {
    $html = Get-Content -LiteralPath $file.FullName -Encoding UTF8 -Raw
    $blocks = [regex]::Split($html, '(?=<div class="message )')

    foreach ($block in $blocks) {
        if ($block -notmatch '^<div class="message ') {
            continue
        }

        $id = $null
        if ($block -match 'id="message(\d+)"') {
            $id = $Matches[1]
        }

        $date = $null
        if ($block -match '(?s)<div class="pull_right date details" title="([^"]+)"') {
            $date = [System.Web.HttpUtility]::HtmlDecode($Matches[1]).Trim()
        }

        $from = $null
        if ($block -match '(?s)<div class="from_name">\s*(.*?)\s*</div>') {
            $from = Convert-HtmlFragmentToText $Matches[1]
            $lastFrom = $from
        }
        elseif ($block -match 'message default clearfix joined') {
            $from = $lastFrom
        }

        if ($from -ne $UserName) {
            continue
        }

        $text = ''
        if ($block -match '(?s)<div class="text">\s*(.*?)\s*</div>') {
            $text = Convert-HtmlFragmentToText $Matches[1]
        }

        if ([string]::IsNullOrWhiteSpace($text)) {
            continue
        }

        $rows.Add([pscustomobject]@{
            id = $id
            date = $date
            sender = $from
            text = $text
            source_file = $file.Name
        })
    }
}

$safeUser = ($UserName -replace '[^\w.-]+', '_')
$csvPath = Join-Path $OutputDir "$safeUser-messages.csv"
$jsonPath = Join-Path $OutputDir "$safeUser-messages.json"
$mdPath = Join-Path $OutputDir "$safeUser-messages.md"

$rows | Export-Csv -LiteralPath $csvPath -NoTypeInformation -Encoding UTF8
$rows | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $jsonPath -Encoding UTF8

$lines = New-Object System.Collections.Generic.List[string]
$lines.Add("# Telegram messages by $UserName")
$lines.Add("")
$lines.Add("Total messages: $($rows.Count)")
$lines.Add("")

foreach ($row in $rows) {
    $lines.Add("## $($row.date) | message$($row.id)")
    $lines.Add("")
    $lines.Add($row.text)
    $lines.Add("")
}

$lines | Set-Content -LiteralPath $mdPath -Encoding UTF8

[pscustomobject]@{
    UserName = $UserName
    MessageCount = $rows.Count
    Csv = $csvPath
    Json = $jsonPath
    Markdown = $mdPath
}
