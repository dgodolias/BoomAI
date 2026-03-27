<p align="center">
  <img src="https://img.shields.io/badge/AI-Gemini_3.1_Pro-4285F4?style=for-the-badge&logo=google&logoColor=white" alt="Gemini 3.1 Pro"/>
  <img src="https://img.shields.io/badge/Language-C%23_/_Unity-239120?style=for-the-badge&logo=csharp&logoColor=white" alt="C# / Unity"/>
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.11+"/>
</p>

<h1 align="center">BoomAI</h1>
<p align="center"><b>AI-powered code fixer for C# / Unity projects</b></p>
<p align="center">One command. Scans your entire codebase. Fixes bugs automatically.</p>

---

## What it does

BoomAI combines **4 static analysis tools** with **Gemini 3.1 Pro** to find and fix real issues in your code:

- **Finds bugs** — race conditions, resource leaks, null refs, async pitfalls
- **Fixes them** — generates exact code replacements and applies them to your files
- **Security audit** — path traversal, auth bypasses, timing attacks, CORS misconfig
- **Zero config** — just `cd` into your project and run `boom-ai fix`

---

## Demo

```
$ cd my-unity-project
$ boom-ai fix

  Scanning codebase...
  214 files found, 180 reviewable
  Languages: C#
  Running static analysis...
  Semgrep: 1 finding(s)
  DevSkim: 0 finding(s)
  Roslyn: 0 finding(s)
  Gitleaks: 0 finding(s)
  Total source: 2,928,651 chars across 214 files
  Sending to Gemini (gemini-3.1-pro-preview)...
  Planning review chunks...
  13 chunk(s) planned
  Reviewing chunk 1/13 (1 files)...
  Reviewing chunk 2/13 (3 files)...
  Reviewing chunk 3/13 (2 files)...
  Chunk 2/13 done — 8 issues (1/13 complete)
  Applied 6 fix(es) to src/UniTask/Assets/Plugins/UniTask/Runtime/UniTask.cs
  Chunk 1/13 done — 5 issues (2/13 complete)
  Applied 4 fix(es) to src/UniTask/Assets/Plugins/UniTask/Runtime/Channel.cs
  ...

  ============================================================
    BoomAI Review
  ============================================================

  99 issue(s) found (4 critical)

  #1 Runtime/Internal/TaskTracker.cs:142 [FIX]
      Race condition: read-modify-write on shared dictionary without lock

  #2 Runtime/UniTask.WhenAll.cs:87 [FIX]
      Task.WhenAll without per-task exception handling

  #3 Runtime/Channel.cs:203 [FIX]
      Thread.Sleep inside async method — use await Task.Delay

  ...

  Done! 93 fix(es) applied. Run `git diff` to see changes.
```

---

## Installation (from scratch)

### 1. Install Python 3.11+

Download from [python.org/downloads](https://www.python.org/downloads/)

> During install, check **"Add Python to PATH"**

Verify:
```
python --version
```

### 2. Install Git

Download from [git-scm.com/downloads](https://git-scm.com/downloads)

> BoomAI uses `git ls-files` to respect your `.gitignore`

### 3. Install BoomAI

```
pip install -e . --user
```

### 4. Install static analysis tools

```
boom-ai install-tools
```

This checks and auto-installs:

| Tool | What it does | Auto-install |
|------|-------------|:---:|
| **Semgrep** | Pattern-based SAST (C# rules + custom Unity rules) | Yes |
| **DevSkim** | Microsoft security analyzer (SARIF) | Yes* |
| **Gitleaks** | Secret/credential detection | Yes |
| **Roslyn** | .NET compiler diagnostics | Manual |

> *DevSkim requires [.NET SDK](https://dotnet.microsoft.com/download). Roslyn also needs it.
> All tools are optional — BoomAI works with whatever is available.

### 5. Get a Gemini API key

1. Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
2. Create a new API key (free tier available)
3. Run:

```
boom-ai settings
```

```
  BoomAI Settings
  ====================================
  [1] Gemini API Key      not set
  [2] Inline comments     OFF
  [3] Explanations        ON

  Enter number to change (q to quit): 1
  Enter new Gemini API key: AIza...
  Key saved.
```

### 6. Fix your code

```
cd your-project
boom-ai fix
```

---

## Commands

### `boom-ai fix`

Scans the **current directory** and auto-applies all fixes.

```
cd my-project          # scan everything
cd my-project/src      # scan only src/
boom-ai fix
```

How it works:
1. Collects all git-tracked files
2. Runs 4 static analysis tools (Semgrep, DevSkim, Roslyn, Gitleaks)
3. Splits code into smart chunks (AI-planned grouping)
4. Sends each chunk to Gemini 3.1 Pro for deep review
5. Applies fixes **as each chunk completes** (no waiting for the full scan)
6. Prints a summary of all issues found

### `boom-ai settings`

Interactive settings menu. Changes are saved to `~/.boomai/.env`.

| Setting | Default | Description |
|---------|---------|-------------|
| **Gemini API Key** | not set | Required. Get one at [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **Inline comments** | OFF | Adds `// BoomAI: fixed resource leak` comments to fixes |
| **Explanations** | ON | Full explanations in findings. Turn OFF to save tokens (= money) |

### `boom-ai install-tools`

Checks which static analysis tools are installed and offers to auto-install missing ones.

```
boom-ai install-tools       # interactive
boom-ai install-tools -y    # auto-install without prompting
```

---

## How it works

```
Your Code
    |
    v
[Static Analysis] -----> Semgrep + DevSkim + Roslyn + Gitleaks
    |
    v
[Smart Chunking] -------> AI plans optimal file groupings
    |
    v
[Gemini 3.1 Pro] -------> Reviews each chunk (3 concurrent)
    |                      Finds bugs, security issues, anti-patterns
    v                      Generates exact code replacements
[Auto-Apply] -----------> Search-and-replace with whitespace tolerance
    |                      Applied per-chunk as they complete
    v
Fixed Code  (run `git diff` to review)
```

### Key technical features

- **AI-planned chunking** — Gemini analyzes your repo structure and groups related files together for better context
- **Incremental apply** — fixes are written to disk as each chunk completes, not at the end
- **Whitespace-tolerant matching** — handles indentation mismatches between AI suggestions and actual code
- **Truncation recovery** — if Gemini's response gets cut off, recovers all complete findings from the partial JSON
- **Fail-safe tools** — if any static analysis tool isn't installed, the scan continues with the others
- **Security-first patterns** — 23 trained C#-specific patterns covering input handling, auth, async/concurrency, and API config

---

## Security patterns checked

BoomAI doesn't just look for generic "security issues". It checks **specific, exploitable patterns**:

**Input Handling**
- Path traversal via URL-encoded characters (`%2e%2e` bypassing validation)
- MIME type trusted from client without server-side magic-byte check
- File paths not normalized before boundary check

**Authentication & Authorization**
- Early return for unknown user (timing attack for username enumeration)
- Plaintext password comparisons
- Inconsistent auth checks across routes
- Auth middleware that doesn't halt execution after 401

**Async & Concurrency (C#-specific)**
- `Thread.Sleep` / `File.ReadAllText` inside async methods
- Read-modify-write without lock (race condition)
- `Task.WhenAll` without per-task exception handling
- Fire-and-forget async calls with swallowed exceptions

**API & Configuration**
- Rate limiting with `skipOnError: true`
- CORS with unvalidated origins
- List endpoints missing pagination
- Inconsistent error response formats

---

## Project structure

```
boomai/
  cli.py               # CLI entry point (fix, settings, install-tools)
  config.py            # Settings (pydantic-settings, env_prefix="BOOMAI_")
  gemini_review.py     # Gemini API + chunking + truncation recovery
  prompts.py           # Dynamic prompt builder + security patterns
  languages.py         # Language detection + extensible skill registry
  models.py            # Finding, ReviewComment, ReviewSummary
  static_analysis.py   # Semgrep, DevSkim, Roslyn, Gitleaks wrappers
  data/
    semgrep/
      unity-rules.yml  # 11 custom C#/Unity Semgrep rules
```

---

## Requirements

- Python 3.11+
- Git
- Gemini API key ([free tier available](https://aistudio.google.com/apikey))
- Optional: .NET SDK 8.0+ (for DevSkim + Roslyn)
