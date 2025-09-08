import os
import glob

def fix_cache_db_paths():
    for fname in glob.glob("*.yaml"):
        with open(fname, "r", encoding="utf-8") as f:
            lines = f.readlines()

        changed = False
        new_lines = []
        for line in lines:
            if line.strip().startswith("cache_db:"):
                key, val = line.split(":", 1)
                val = val.strip()
                if val.startswith("../"):
                    val = val[3:]  # зрізаємо "../"
                    line = f"{key}: {val}\n"
                    changed = True
            new_lines.append(line)

        if changed:
            with open(fname, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
            print(f"✅ Fixed {fname}")
        else:
            print(f"ℹ️ No changes in {fname}")

if __name__ == "__main__":
    fix_cache_db_paths()
