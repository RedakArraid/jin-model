param(
    [Parameter(Mandatory = $true)]
    [string]$TargetDirectory,
    [Parameter(Mandatory = $true)]
    [string]$ReportDirectory
)

$ErrorActionPreference = 'Stop'
$target = (Resolve-Path -LiteralPath $TargetDirectory).Path
if (-not (Test-Path -LiteralPath $target -PathType Container)) {
    throw "Target directory does not exist: $target"
}

New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$report = (Resolve-Path -LiteralPath $ReportDirectory).Path
$separator = [IO.Path]::DirectorySeparatorChar
if ($report.StartsWith($target + $separator, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The report/quarantine directory must not be inside the source corpus.'
}

$quarantinePath = Join-Path $report 'duplicate_quarantine'
New-Item -ItemType Directory -Path $quarantinePath -Force | Out-Null
$quarantine = (Resolve-Path -LiteralPath $quarantinePath).Path

$files = @(Get-ChildItem -LiteralPath $target -File -Filter '*.pdf' | Sort-Object Name, FullName)
$hashed = [Collections.Generic.List[object]]::new()
$index = 0
foreach ($file in $files) {
    $index += 1
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    $hashed.Add([pscustomobject]@{
        SHA256 = $hash
        FullName = $file.FullName
        Name = $file.Name
        Length = $file.Length
    })
    if ($index % 250 -eq 0) {
        Write-Output "hashed=$index/$($files.Count)"
    }
}

$manifest = [Collections.Generic.List[object]]::new()
$inventory = [Collections.Generic.List[object]]::new()
foreach ($group in ($hashed | Group-Object SHA256)) {
    $ordered = @($group.Group | Sort-Object Name, FullName)
    $keep = $ordered[0]
    $inventory.Add([pscustomobject]@{
        SHA256 = $keep.SHA256
        Path = $keep.FullName
        Name = $keep.Name
        Length = $keep.Length
    })
    foreach ($duplicate in ($ordered | Select-Object -Skip 1)) {
        if (-not $duplicate.FullName.StartsWith(
            $target + $separator,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing source outside corpus: $($duplicate.FullName)"
        }
        if ($duplicate.Length -ne $keep.Length) {
            throw "Inconsistent length for SHA $($group.Name)"
        }
        $destinationName = "$($group.Name)_$($duplicate.Name)"
        $destination = Join-Path $quarantine $destinationName
        if (-not $destination.StartsWith(
            $quarantine + $separator,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw "Refusing destination outside quarantine: $destination"
        }
        if (Test-Path -LiteralPath $destination) {
            throw "Quarantine destination already exists: $destination"
        }
        $manifest.Add([pscustomobject]@{
            SHA256 = $group.Name
            KeptPath = $keep.FullName
            RemovedFromCorpus = $duplicate.FullName
            QuarantinePath = $destination
            Length = $duplicate.Length
            Status = 'PLANNED'
        })
    }
}

$manifestPath = Join-Path $report 'dedup_manifest.csv'
$inventoryPath = Join-Path $report 'unique_inventory.csv'
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8 -Delimiter ';'
$inventory | Sort-Object Name | Export-Csv -LiteralPath $inventoryPath -NoTypeInformation -Encoding UTF8 -Delimiter ';'

$moved = 0
foreach ($row in $manifest) {
    if (-not (Test-Path -LiteralPath $row.RemovedFromCorpus -PathType Leaf)) {
        throw "Source disappeared before move: $($row.RemovedFromCorpus)"
    }
    Move-Item -LiteralPath $row.RemovedFromCorpus -Destination $row.QuarantinePath
    if (Test-Path -LiteralPath $row.RemovedFromCorpus) {
        throw "Source still exists after move: $($row.RemovedFromCorpus)"
    }
    if (-not (Test-Path -LiteralPath $row.QuarantinePath -PathType Leaf)) {
        throw "Quarantine copy missing after move: $($row.QuarantinePath)"
    }
    $row.Status = 'QUARANTINED'
    $moved += 1
}
$manifest | Export-Csv -LiteralPath $manifestPath -NoTypeInformation -Encoding UTF8 -Delimiter ';'

$remaining = @(Get-ChildItem -LiteralPath $target -File -Filter '*.pdf')
Write-Output "source_before=$($files.Count)"
Write-Output "duplicates_quarantined=$moved"
Write-Output "source_after=$($remaining.Count)"
Write-Output "manifest=$manifestPath"
Write-Output "inventory=$inventoryPath"
Write-Output "quarantine=$quarantine"
