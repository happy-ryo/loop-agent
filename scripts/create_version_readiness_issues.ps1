$ErrorActionPreference = "Stop"

$Repo = "happy-ryo/loop-agent"
$Issues = @(
  @{
    Title = "Prepare 0.1.1 docs consistency release"
    Body = ".github/ISSUE_DRAFTS/001-docs-consistency-0-1-1.md"
  },
  @{
    Title = "Define the stable public API boundary before 1.0"
    Body = ".github/ISSUE_DRAFTS/002-public-api-boundary-0-2.md"
  },
  @{
    Title = "Lock down CLI and state.db compatibility contracts"
    Body = ".github/ISSUE_DRAFTS/003-cli-persistence-contract.md"
  },
  @{
    Title = "Align release metadata and maturity policy"
    Body = ".github/ISSUE_DRAFTS/004-release-metadata-policy.md"
  },
  @{
    Title = "Run the 1.0.0 release readiness gate"
    Body = ".github/ISSUE_DRAFTS/005-1-0-release-gate.md"
  }
)

foreach ($Issue in $Issues) {
  $Existing = gh issue list --repo $Repo --state open --limit 200 --json title,url | ConvertFrom-Json
  $Match = $Existing | Where-Object { $_.title -eq $Issue.Title } | Select-Object -First 1
  if ($Match) {
    Write-Output "exists: $($Match.url)"
    continue
  }
  gh issue create --repo $Repo --title $Issue.Title --body-file $Issue.Body
}
