#!/usr/bin/env python3
import subprocess
import sys
import questionary

def run_cmd(cmd):
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    if result.returncode != 0:
        print(f"Error executing: {cmd}\n{result.stderr}")
        sys.exit(1)
    return result.stdout.strip()

def get_modified_files():
    output = run_cmd("git status --porcelain")
    if not output:
        return []
    files = []
    for line in output.splitlines():
        # First 2 chars are status code (e.g. " M", "M ", "??")
        filename = line[3:].strip()
        files.append(filename)
    return files

def main():
    files = get_modified_files()
    if not files:
        print("No modified or untracked files found.")
        return

    # 1. Multi-select files
    selected_files = questionary.checkbox(
        "Select files to stage and commit:",
        choices=files
    ).ask()

    if not selected_files:
        print("No files selected. Aborting.")
        return

    # Default git configuration values
    default_email = run_cmd("git config user.email") or ""
    default_name = run_cmd("git config user.name") or ""

    # 2. Input commit details
    commit_summary = questionary.text("Commit summary line:").ask()
    if not commit_summary:
        print("Commit summary is required.")
        return

    author_email = questionary.text("Author email:", default=default_email).ask()
    
    raw_bullets = questionary.text(
        "Bullet points (separate entries with commas, or leave blank):"
    ).ask()

    # 3. Format strict commit message
    message_lines = [commit_summary.strip(), ""]

    if raw_bullets and raw_bullets.strip():
        bullets = [b.strip() for b in raw_bullets.split(",") if b.strip()]
        for bullet in bullets:
            message_lines.append(f"- {bullet}")
        message_lines.append("")

    sign_off_name = default_name if default_name else "Author"
    message_lines.append(f"Signed-off-by: {sign_off_name} <{author_email}>")

    full_commit_msg = "\n".join(message_lines)

    print("\n--- Generated Commit Message ---")
    print(full_commit_msg)
    print("--------------------------------\n")

    confirm = questionary.confirm("Stage, commit, and push now?").ask()
    if not confirm:
        print("Operation cancelled.")
        return

    # 4. Git Add, Commit, Push
    print("Staging selected files...")
    for file in selected_files:
        run_cmd(f'git add "{file}"')

    print("Committing...")
    subprocess.run(["git", "commit", "-m", full_commit_msg], check=True)

    print("Pushing to remote...")
    subprocess.run(["git", "push"], check=True)
    print("Successfully committed and pushed!")

if __name__ == "__main__":
    main()