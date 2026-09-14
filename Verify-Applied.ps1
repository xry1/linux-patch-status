param(
    [string]$AuthorEmail = 'runyu.xiao@seu.edu.cn',
    [string]$AuthorName = 'Runyu Xiao',
    [string]$Proxy,
    [string]$Output = (Join-Path $PSScriptRoot 'applied_verifications.json'),
    [string[]]$AdditionalCommit = @(),
    [string[]]$RevertCommit = @(),
    [switch]$RevertsOnly
)
$ErrorActionPreference = 'Stop'

function Read-GitHubJson([string]$Url) {
    $request = @{ Uri = $Url; Headers = @{'User-Agent'='linux-patch-status-calibration'}; TimeoutSec = 40; ErrorAction = 'Stop' }
    if ($Proxy) { $request.Proxy = $Proxy }
    Invoke-RestMethod @request
}

$previous = $null
if (Test-Path -LiteralPath $Output) {
    $previous = Get-Content -Raw -LiteralPath $Output | ConvertFrom-Json
    if ($previous.schema_version -ne 1 -or $previous.repository -ne 'torvalds/linux' -or $previous.author_email -ine $AuthorEmail) {
        throw 'Existing evidence belongs to a different repository or author'
    }
}
if ($RevertsOnly) {
    if (!$previous -or !$RevertCommit.Count) { throw 'RevertsOnly requires an existing snapshot and RevertCommit' }
    $head = $previous.head_sha
    $candidates = @($RevertCommit | Sort-Object -Unique)
} else {
    $head = (Read-GitHubJson 'https://api.github.com/repos/torvalds/linux/branches/master').commit.sha
    $query = 'repo:torvalds/linux "Signed-off-by: ' + $AuthorName + ' <' + $AuthorEmail + '>"'
    $queryUrl = 'https://api.github.com/search/commits?q=' + [Uri]::EscapeDataString($query) + '&per_page=100'
    $search = Read-GitHubJson $queryUrl
    if ($search.incomplete_results -or $search.total_count -gt 1000) { throw 'Incomplete commit search; previous evidence preserved' }
    $candidates = @($search.items.sha)
    for ($page = 2; $candidates.Count -lt $search.total_count; $page++) {
        $more = Read-GitHubJson ($queryUrl + '&page=' + $page)
        if ($more.incomplete_results -or !$more.items.Count) { throw 'Incomplete commit search page' }
        $candidates += @($more.items.sha)
    }
    # Previously verified reversals must survive a normal refresh of the snapshot.
    $candidates = @(@($candidates) + $AdditionalCommit + $RevertCommit + @($previous.commits.sha) + @($previous.reverts.sha) |
        Where-Object { $_ } | Sort-Object -Unique)
}
if ($head -notmatch '^[0-9a-f]{40}$') { throw 'Invalid mainline head' }
$signature = '(?im)^Signed-off-by:\s*' + [regex]::Escape($AuthorName) + '\s*<' + [regex]::Escape($AuthorEmail) + '>\s*$'
$verified = @()
foreach ($sha in $candidates) {
    if ($sha -notmatch '^[0-9a-f]{40}$') { throw 'Invalid commit hash' }
    $url = 'https://api.github.com/repos/torvalds/linux/compare/' + $sha + '...' + $head + '?per_page=1'
    $comparison = Read-GitHubJson $url
    $commit = $comparison.base_commit
    $ancestor = $comparison.status -in @('ahead','identical') -and $comparison.behind_by -eq 0 -and $comparison.merge_base_commit.sha -eq $sha -and $commit.sha -eq $sha
    $signed = $commit.commit.message -match $signature
    $authored = $commit.commit.author.email -ieq $AuthorEmail
    $revertMatch = [regex]::Match($commit.commit.message, '(?im)^This reverts commit\s+([0-9a-f]{40})\.')
    $revertsCommit = if ($revertMatch.Success) { $revertMatch.Groups[1].Value.ToLowerInvariant() } else { $null }
    if ($RevertsOnly -or $sha -in $RevertCommit) {
        if (!$ancestor -or !$revertsCommit -or $revertsCommit -notin (@($previous.commits.sha) + @($previous.reverts.sha) + $candidates)) {
            throw ('No verified reversal of a known commit: ' + $sha)
        }
    }
    $verified += [ordered]@{
        sha = $sha
        title = ($commit.commit.message -split "`n")[0]
        author = $commit.commit.author.name
        author_email = $commit.commit.author.email
        authored_by_user = $authored
        signed_off_by_user = $signed
        committed_at = $commit.commit.committer.date
        mainline_ancestor = $ancestor
        merge_base = $comparison.merge_base_commit.sha
        behind_by = $comparison.behind_by
        head_sha = $head
        source = 'https://github.com/torvalds/linux/commit/' + $sha
        comparison_url = $url
        checked_at = [DateTimeOffset]::Now.ToString('o')
        message = $commit.commit.message
        reverts_commit = $revertsCommit
    }
    Write-Host ($verified.Count.ToString() + '/' + $candidates.Count + ' ' + $sha.Substring(0,12) + ' mainline=' + $ancestor + ' attribution=' + ($signed -or $authored))
}
if ($RevertsOnly) {
    $snapshot = $previous
    $combined = @(@($previous.reverts | Where-Object { $_ -and $_.sha -notin $candidates }) + $verified)
    $snapshot | Add-Member -NotePropertyName reverts -NotePropertyValue $combined -Force
    $snapshot | Add-Member -NotePropertyName reverts_checked_at -NotePropertyValue ([DateTimeOffset]::Now.ToString('o')) -Force
} else {
$snapshot = [ordered]@{
    schema_version = 1
    author_email = $AuthorEmail
    repository = 'torvalds/linux'
    head_sha = $head
    checked_at = [DateTimeOffset]::Now.ToString('o')
    search_url = $queryUrl
    search_total = $search.total_count
    search_complete = $true
    commits = $verified
    reverts = @($verified | Where-Object { $_.reverts_commit })
    reverts_checked_at = [DateTimeOffset]::Now.ToString('o')
}
}
$absoluteOutput = [IO.Path]::GetFullPath($Output)
$temporaryOutput = $absoluteOutput + '.tmp'
[IO.File]::WriteAllText($temporaryOutput, ($snapshot | ConvertTo-Json -Depth 12) + "`n", [Text.UTF8Encoding]::new($false))
Move-Item -LiteralPath $temporaryOutput -Destination $absoluteOutput -Force
Write-Host ('Verified evidence saved: ' + $absoluteOutput)
