# Publish a BluePeak TAG Master release to GitHub Releases.
#
#   1. Build first:   .\build-release.ps1 -BaseUrl "https://github.com/randallmoore397/tag-master/releases/latest/download"
#   2. Publish:       .\publish-github.ps1 -Token $env:GITHUB_TOKEN
#
# The app's update feed then becomes:
#   https://github.com/randallmoore397/tag-master/releases/latest/download/update-manifest.json
#
# The token needs "Contents: Read and write" (fine-grained) or the "repo"
# scope (classic). It is only used for this run and is never written to disk.

[CmdletBinding()]
param(
    [string]$Token = $env:GITHUB_TOKEN,
    [string]$Repo = "randallmoore397/tag-master",
    [string]$Notes = "",
    [switch]$Prerelease
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    if (-not $Token) { throw "Pass -Token <github token> or set GITHUB_TOKEN." }

    $match = Select-String -Path "bluepeak_tag_master.py" -Pattern '^APP_VERSION = "([^"]+)"' |
        Select-Object -First 1
    if (-not $match) { throw "Could not read APP_VERSION." }
    $version = $match.Matches[0].Groups[1].Value
    $tag = "v$version"

    $setup = Join-Path $root "Output\BluePeak-TAG-Master-Setup.exe"
    $manifest = Join-Path $root "Output\update-manifest.json"
    foreach ($f in @($setup, $manifest)) {
        if (-not (Test-Path $f)) { throw "Missing $f. Run build-release.ps1 first." }
    }

    $headers = @{
        Authorization          = "Bearer $Token"
        "User-Agent"           = "BluePeak-TAG-Master"
        Accept                 = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }

    $body = @{
        tag_name         = $tag
        target_commitish = "main"
        name             = "BluePeak TAG Master $version"
        body             = if ($Notes) { $Notes } else { "BluePeak TAG Master $version" }
        draft            = $false
        prerelease       = [bool]$Prerelease
    } | ConvertTo-Json

    $release = $null
    try {
        $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases" `
            -Headers $headers -Method Post -Body $body -ContentType "application/json"
        Write-Host "Created release $tag."
    }
    catch {
        $detail = $_.ErrorDetails.Message
        if ($detail -match "already_exists") {
            $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/tags/$tag" -Headers $headers
            Write-Host "Release $tag already exists; adding or replacing assets."
            foreach ($a in @($release.assets)) {
                Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/assets/$($a.id)" `
                    -Headers $headers -Method Delete | Out-Null
            }
        }
        else { throw "Could not create release: $detail" }
    }

    function Send-Asset($file, $name) {
        $bytes = [IO.File]::ReadAllBytes($file)
        $upload = "https://uploads.github.com/repos/$Repo/releases/$($release.id)/assets?name=$name"
        Invoke-RestMethod -Uri $upload -Headers $headers -Method Post -Body $bytes `
            -ContentType "application/octet-stream" | Out-Null
        Write-Host "Uploaded $name ($([math]::Round($bytes.Length / 1MB, 1)) MB)."
    }

    Send-Asset $setup "BluePeak-TAG-Master-Setup.exe"
    Send-Asset $manifest "update-manifest.json"

    $base = "https://github.com/$Repo/releases/latest/download"
    Write-Host ""
    Write-Host "Published $tag."
    Write-Host "  Installer : $base/BluePeak-TAG-Master-Setup.exe"
    Write-Host "  Feed URL  : $base/update-manifest.json"
    Write-Host ""
    Write-Host "Paste the feed URL into Settings > App updates."
}
finally {
    Pop-Location
}
