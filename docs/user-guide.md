# prview User Guide

*Review GitHub pull requests in your browser, file by file, with AI help.*

**Last updated:** 2026-06-23 · **Applies to:** prview 0.1.0

---

## Table of Contents

1. [What is prview?](#what-is-prview)
2. [Who should use this?](#who-should-use-this)
3. [Before you start (prerequisites)](#before-you-start-prerequisites)
4. [Install and launch](#install-and-launch)
5. [Loading a pull request](#loading-a-pull-request)
6. [The review workspace](#the-review-workspace)
7. [Using the AI panel](#using-the-ai-panel)
8. [Marking files viewed](#marking-files-viewed)
9. [Flagging a file](#flagging-a-file)
10. [Commenting on a file](#commenting-on-a-file)
11. [Submitting your review](#submitting-your-review)
12. [Resuming a review later](#resuming-a-review-later)
13. [Keyboard shortcuts](#keyboard-shortcuts)
14. [A note on security](#a-note-on-security)
15. [Troubleshooting](#troubleshooting)

---

## What is prview?

prview is a small app that runs on your own computer and opens in your web browser. It lets you review a GitHub pull request (PR) one file at a time, with a clear side-by-side view of what changed and an AI assistant that can summarize, explain, and answer questions about each file.

Think of it as a calmer, more focused way to read a PR than scrolling the GitHub web page: you move through files in order, the AI tells you what each change does, and the app remembers where you left off.

**Use prview when you want to:**

- Walk through a PR carefully, file by file, without losing your place.
- Get a quick AI summary or explanation of an unfamiliar change.
- Mark files as viewed, flag ones that need attention, leave comments, and submit your approval — all from one screen.
- Pick up a half-finished review days later, exactly where you stopped.

prview uses the GitHub and AI command-line tools you already have set up, so there are no new accounts, passwords, or API keys to manage.

---

## Who should use this?

prview is for developers who review pull requests. You do not need to be a backend or web expert to use it — if you can run a command in a terminal and open a browser, you can use prview.

It is built for one person reviewing one PR at a time on their own machine. It is not a team dashboard or a hosted service.

---

## Before you start (prerequisites)

prview leans on tools you likely already have. Make sure these three are installed and working:

📝 **What you'll need:**

1. **The GitHub CLI (`gh`)** — installed and signed in.
   - Check it works: run `gh auth status` in your terminal.
   - If it says you are not logged in, run `gh auth login` and follow the prompts.

2. **The Claude CLI (`claude`)** — available on your machine. This powers the AI summary, explain, and ask features.

3. **Python 3.10+ and `uv`** — used to install and start prview.

💡 **Tip:** prview never asks you for a GitHub token or an AI key. It simply reuses the `gh` and `claude` tools' own logins. If those two commands work for you on their own, prview will work too.

---

## Install and launch

From inside the `prview` project folder:

1. **Install the dependencies** (one time):

   ```
   uv sync
   ```

2. **Start prview:**

   ```
   uv run prview
   ```

   (If you prefer, `python -m prview` does exactly the same thing.)

That's it. prview will:

- Pick a free port on your own computer.
- Create a fresh, one-time session token for security.
- Open your default browser automatically at the right address.

✅ **What you should see:** a new browser tab opens showing the prview landing screen, with a box titled **Load a Pull Request**.

⚠️ **Note:** There is no `./prview` script to run directly — the folder named `prview` holds the app code itself. Always start it with `uv run prview` or `python -m prview`.

![The prview landing screen](screenshots/01-landing.png)

---

## Loading a pull request

On the landing screen, type the PR you want to review into the **Load a Pull Request** box. prview accepts two formats:

- A short reference like `owner/repo#123`
- A full GitHub PR URL like `https://github.com/owner/repo/pull/123`

![Landing screen with a PR reference typed in](screenshots/02-landing-ref-filled.png)

Then click **Load PR** (or just press **Enter**).

💡 **Tip:** If you paste a plain number with no repository, prview will ask you to use the full `owner/repo#123` form — it needs to know which repository the PR lives in.

After a moment, the review workspace opens.

---

## The review workspace

This is where you'll spend most of your time. The screen is split into three areas:

- **Top bar (PR summary):** the PR title, author, the branches involved (`base ← head`), file count, lines added and removed, the CI status, and the current review decision. The **Submit** button lives here too.
- **Left sidebar (file list):** every changed file, sorted with the biggest changes first. Each row shows the filename and its added/removed line counts. A small marker shows which file you're currently on, and badges appear for files you've viewed (`✓`) or flagged (`⚑`). A progress indicator at the top shows how many files you've viewed.
- **Main area (file detail):** the AI panel at the top, the side-by-side diff below it, and an action bar at the bottom.

![The review workspace: PR summary, file list, and side-by-side diff](screenshots/03-review-loaded.png)

The diff is shown **side by side** — the old version on the left, the new version on the right — so additions and removals are easy to spot.

![A file's changes shown side by side](screenshots/05-diff-sidebyside.png)

To move between files, click a file in the sidebar, or use the keyboard: **`j`** for the next file and **`k`** (or **`b`**) for the previous one.

---

## Using the AI panel

The AI panel sits at the top of the main area and helps you understand each file without reading every line.

- **Summary (automatic):** when you open a file, prview automatically asks the AI for a short summary of what changed.
- **Explain (`e`):** click **Explain** for a deeper walkthrough of the file's changes.
- **Ask (`a`):** type your own question about the current file and get an answer.

Because the AI can take a little while (occasionally up to a few minutes), the panel shows a loading state with a timer and a **Cancel** button while it works — so you're never left guessing.

![The AI panel working on an Explain request](screenshots/04-ai-explain-loading.png)

✅ **What you should see:** a spinner and elapsed timer while the AI thinks, then the answer text appears in the panel.

⚠️ **If something goes wrong** (for example, the AI tool times out), the panel shows a clear error message with a **Retry** button. Just click **Retry** to try again.

---

## Marking files viewed

When you've finished reading a file, mark it as viewed so you can track your progress.

- Click **Viewed** in the action bar, or press **`v`**.

The file gets a `✓` badge in the sidebar and the viewed count goes up. prview saves this both locally and back to GitHub, so your "viewed" marks show up on GitHub too.

💡 **Tip:** If GitHub can't be reached at that moment, prview still records the file as viewed locally and tells you the GitHub sync didn't go through — your progress is never lost.

---

## Flagging a file

Flagging is a private, local-only note to yourself: "come back to this one." It does **not** post anything to GitHub.

1. Click **Flag** in the action bar, or press **`f`**.
2. Optionally type a note explaining why you flagged it.
3. Click **Flag ⚑** to save.

![The flag dialog with an optional note](screenshots/06-flag-modal.png)

The file gets a `⚑` badge in the sidebar. If you open the flag dialog again on an already-flagged file, you'll see your note and an option to **Unflag** it.

Flagged files (and their notes) are gathered together for you on the submit screen, so they're easy to mention in your final review.

---

## Commenting on a file

To leave a comment about the file you're currently viewing:

1. Click **Comment** in the action bar, or press **`c`**.
2. Type your comment in the box.
3. Click **Post comment**.

![The comment dialog for the current file](screenshots/08-comment-modal.png)

Your comment is posted to the PR on GitHub, with the filename included so others know which file you're referring to.

💡 **Tip:** To close any dialog without doing anything, click **Cancel** or press **`Esc`** (or **`q`**).

---

## Submitting your review

When you're ready to finish, click **Submit** in the top bar (or press **`s`**). This opens the submit screen.

![The submit review screen with counts, flagged files, and decision options](screenshots/09-submit-review.png)

Here you'll see:

- **Counts** at the top: total files, how many you viewed, how many you flagged, and how many you skipped, plus the number of comments you posted.
- **Flagged files** listed in a table with their notes — a handy checklist of what you wanted to highlight.
- A **Review body** box where you can write an optional summary message for the whole review.
- A **Decision** choice: **Approve**, **Request changes**, or **Comment only**.

If you still have unviewed files, prview shows a friendly, non-blocking reminder ("X files not yet viewed — submit anyway?"). You can still submit if you're ready.

Click **Submit review** to send it to GitHub. Once submitted, prview marks this review as done — and the PR will show a **DONE** badge on the landing screen's resume list.

---

## Resuming a review later

You can close the browser tab at any time. prview saves your progress — viewed files, flags, notes, and comment counts — every time you take an action.

The next time you start prview, the landing screen lists any in-progress reviews under **Resume in-progress reviews**. Each row shows the PR reference and a quick status like `0 viewed · 1 flagged`.

![The landing screen showing a resumable review](screenshots/10-landing-resume-row.png)

Click the row to jump straight back into that review, exactly where you left off.

💡 **Tip:** The auto-summary will re-run for the file you reopen. If the AI is unavailable at that moment, you'll simply see the error-with-Retry state — your review progress itself is untouched.

![A resumed review showing the AI panel's error-and-retry state](screenshots/11-resumed-review-ai-error.png)

---

## Keyboard shortcuts

prview is built to be driven from the keyboard. Each shortcut is a single key:

| Key | Action |
|-----|--------|
| `v` | Mark current file viewed |
| `e` | Explain current file (AI) |
| `a` | Ask a question about the current file (AI) |
| `c` | Comment on the current file |
| `f` | Flag / unflag the current file |
| `s` | Open the submit-review screen |
| `j` | Go to the next file |
| `k` or `b` | Go to the previous file |
| `q` | Go back / close the current dialog |

The underlined letter on each on-screen button reminds you of its shortcut.

---

## A note on security

prview runs a small web server on your computer, and that server can act on your behalf (posting to GitHub, running the AI tool). To keep other programs and websites from reaching it, prview:

- Listens **only** on your own machine (`127.0.0.1`) — never on the open network.
- Requires a **one-time session token** that's built into the address it opens. Requests without it are rejected.
- Checks that requests are coming from prview's own page, not a stray website.

⚠️ **Don't share or forward the port** prview runs on, and don't try to expose it to the internet. It's meant to stay private to your computer.

---

## Troubleshooting

**The browser didn't open, or I see a "not authorized" message.**
prview opens a special address that includes your session token (it looks like `...?token=...`). Always use the address prview opened for you. If you typed the address by hand without the token, go back to your terminal and open the full link it printed.

**Loading a PR fails with a GitHub error.**
Make sure you're signed in: run `gh auth status`, and if needed, `gh auth login`. prview will usually show a hint like "run `gh auth login`" when this is the problem.

**"Enter `owner/repo#123` or a PR URL."**
You probably entered just a number. prview needs the repository too — use the `owner/repo#123` form or paste the full GitHub PR URL.

**The AI summary or explanation shows an error.**
The AI tool (`claude`) may have timed out or be unavailable. Click **Retry**. If it keeps failing, confirm the `claude` command works on its own in your terminal.

**A file shows "binary file changed" instead of a diff.**
That's expected for files that aren't plain text (images, compiled files, and so on) — there's no line-by-line diff to show.

**I marked a file viewed but it didn't sync to GitHub.**
prview still saved it locally and will tell you the GitHub sync failed. Your progress is safe; the viewed mark just may not appear on GitHub until connectivity returns.
