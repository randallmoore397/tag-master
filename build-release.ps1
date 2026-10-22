# Build a shareable BluePeak TAG Master installer and its update manifest.
#
#   .\build-release.ps1 -BaseUrl "https://your-site.example/downloads"
#
# The installer is written to Output\BluePeak-TAG-Master-Setup.exe.
# Upload that file and Output\update-manifest.json to the folder used for
# BaseUrl, then paste the manifest URL into Settings > App updates.
#
# The build folder (dist-release) never contains the live database, and the
# installer is told never to overwrite *.db or the backups folder.

[CmdletBinding()]
param(
    [string]$BaseUrl = "",
    [string]$Notes = "",
    [string]$Python = "py",
    [string]$Iscc = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $root
try {
    $match = Select-String -Path "bluepeak_tag_master.py" -Pattern '^APP_VERSION = "([^"]+)"' |
        Select-Object -First 1
    if (-not $match) { throw "Could not read APP_VERSION from bluepeak_tag_master.py." }
    $version = $match.Matches[0].Groups[1].Value

    $notesText = if ($Notes) { $Notes } else {
        "BluePeak TAG Master $version"
    }
    $base = if ($BaseUrl) { $BaseUrl.TrimEnd("/") } else { "https://example.com/downloads" }
    if ($base -notmatch '^https://' -and $base -notmatch '^http://(127\.0\.0\.1|localhost)') {
        throw "BaseUrl must use https:// (or localhost for testing)."
    }

    Write-Host "Building BluePeak TAG Master $version ..."
    $distName = "dist-release"
    if (Test-Path $distName) { Remove-Item $distName -Recurse -Force }

    if ($Python -eq "py") {
        & py -3 -m PyInstaller --clean --noconfirm --distpath $distName BluePeak-TAG-Master.spec
    } else {
        & $Python -m PyInstaller --clean --noconfirm --distpath $distName BluePeak-TAG-Master.spec
    }
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    if (-not $Iscc) {
        $cmd = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($cmd) { $Iscc = $cmd.Source }
        else {
            foreach ($p in @(
                "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
                "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                "$env:ProgramFiles\Inno Setup 5\ISCC.exe",
                "${env:ProgramFiles(x86)}\Inno Setup 5\ISCC.exe"
            )) {
                if (Test-Path $p) { $Iscc = $p; break }
            }
        }
    }
    if (-not $Iscc) {
        throw "Inno Setup was not found. Install Inno Setup 6 or pass -Iscc <path to ISCC.exe>."
    }

    Write-Host "Building the installer ..."
    & $Iscc "/DBuildDir=$distName" "/DAppVersion=$version" "BluePeak-TAG-Master.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed." }

    $setup = Join-Path $root "Output\BluePeak-TAG-Master-Setup.exe"
    if (-not (Test-Path $setup)) { throw "The installer was not produced at $setup." }

    $hash = (Get-FileHash -Algorithm SHA256 $setup).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        version = $version
        url     = "$base/BluePeak-TAG-Master-Setup.exe"
        sha256  = $hash
        notes   = $notesText
    }
    $manifestPath = Join-Path $root "Output\update-manifest.json"
    $manifest | ConvertTo-Json -Depth 3 | Set-Content -Path $manifestPath -Encoding UTF8

    Write-Host ""
    Write-Host "Done."
    Write-Host "  Installer : $setup"
    Write-Host "  Manifest  : $manifestPath"
    Write-Host "  Version   : $version"
    Write-Host "  SHA-256   : $hash"
    Write-Host ""
    Write-Host "Upload the installer and manifest to: $base"
    Write-Host "Then set the manifest URL in Settings > App updates."
}
finally {
    Pop-Location
}
