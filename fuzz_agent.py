"""
fuzz_agent.py — Smart Contract Fuzzing Agent
يعمل داخل GitHub Actions بدون Anthropic API key.
يستخدم Foundry (forge) لاختبار العقود الذكية.
يرسل النتائج على Telegram فقط عند اكتشاف Bug/Crash.
"""

import os
import json
import random
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# إعدادات من Environment Variables
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPOSITORY", "unknown/repo")
GITHUB_RUN_ID      = os.environ.get("GITHUB_RUN_ID", "0")
GITHUB_SERVER_URL  = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

ARTIFACT_LINK = f"{GITHUB_SERVER_URL}/{GITHUB_REPO}/actions/runs/{GITHUB_RUN_ID}"

MAX_ITERATIONS = 80          # حد الـ iterations داخل وقت الـ runner
TIME_LIMIT_SEC = 18000       # 5 ساعات (أقل من 6h حد الـ runner)
POC_DIR        = Path("poc")
REPORT_FILE    = Path("fuzz_report.json")


# ─────────────────────────────────────────────
# إرسال Telegram
# ─────────────────────────────────────────────
def send_to_telegram(message: str, poc_file: Path | None = None) -> None:
    """يرسل رسالة نصية + ملف PoC اختياري على Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram secrets غير موجودة، سيتم تخطي الإرسال.")
        return

    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

    try:
        # أرسل الرسالة النصية
        resp = requests.post(
            f"{base_url}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "Markdown",
            },
            timeout=15,
        )
        resp.raise_for_status()
        print("[INFO] تم إرسال إشعار Telegram.")

        # أرسل الملف إذا وجد
        if poc_file and poc_file.exists():
            with open(poc_file, "rb") as f:
                file_resp = requests.post(
                    f"{base_url}/sendDocument",
                    data={"chat_id": TELEGRAM_CHAT_ID},
                    files={"document": (poc_file.name, f, "text/plain")},
                    timeout=30,
                )
            file_resp.raise_for_status()
            print(f"[INFO] تم رفع الملف: {poc_file.name}")

    except Exception as e:
        print(f"[ERROR] فشل إرسال Telegram: {e}")


# ─────────────────────────────────────────────
# توليد العقود الذكية المختبَرة (Targets)
# ─────────────────────────────────────────────
def write_fuzz_contracts() -> None:
    """يكتب عقود Solidity للـ fuzzing في مجلد src/."""
    src = Path("src")
    src.mkdir(exist_ok=True)

    # عقد يحتوي على Reentrancy vulnerability
    (src / "VulnerableBank.sol").write_text("""
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableBank {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    // ⚠️ Reentrancy vulnerability intentional for fuzzing
    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "No balance");
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "Transfer failed");
        balances[msg.sender] = 0;   // ← state update AFTER call
    }

    function getBalance() external view returns (uint256) {
        return address(this).balance;
    }
}
""")

    # عقد يحتوي على Integer Overflow
    (src / "UnsafeMath.sol").write_text("""
// SPDX-License-Identifier: MIT
pragma solidity ^0.7.6;   // <0.8 → no built-in overflow check

contract UnsafeMath {
    uint256 public totalSupply;

    function mint(uint256 amount) external {
        totalSupply += amount;  // ⚠️ Overflow possible pre-0.8
    }
}
""")

    print("[INFO] كتابة عقود Solidity للاختبار.")


# ─────────────────────────────────────────────
# توليد اختبارات Fuzz ديناميكية
# ─────────────────────────────────────────────
def generate_fuzz_test(iteration: int, seed: int) -> Path:
    """يولد ملف Solidity فuzz test جديد لكل iteration."""
    test_dir = Path("test")
    test_dir.mkdir(exist_ok=True)

    test_file = test_dir / f"FuzzRound_{iteration}.t.sol"

    # Semantic mutations بناءً على الـ seed
    random.seed(seed)
    amount_range  = random.choice(["1 ether", "0.001 ether", "type(uint256).max", "0"])
    attack_repeat = random.randint(1, 5)
    use_reentrancy = random.random() > 0.5

    attacker_body = ""
    if use_reentrancy:
        attacker_body = f"""
    uint8 private _depth;
    VulnerableBank private _bank;

    constructor(address bank) {{ _bank = VulnerableBank(bank); }}

    receive() external payable {{
        if (_depth < {attack_repeat} && address(_bank).balance > 0) {{
            _depth++;
            _bank.withdraw();
        }}
    }}

    function attack() external payable {{
        _bank.deposit{{value: msg.value}}();
        _bank.withdraw();
    }}
"""
    else:
        attacker_body = """
    // Dummy attacker – no reentrancy
    function attack() external payable {}
"""

    test_file.write_text(f"""
// SPDX-License-Identifier: MIT
// Auto-generated fuzz test — Iteration {iteration} — Seed {seed}
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/VulnerableBank.sol";

contract Attacker {{
    {attacker_body}
}}

contract FuzzRound_{iteration} is Test {{
    VulnerableBank bank;
    Attacker attacker;

    function setUp() public {{
        bank     = new VulnerableBank();
        attacker = new Attacker(address(bank));
        vm.deal(address(bank), 10 ether);
        vm.deal(address(attacker), 1 ether);
    }}

    /// @notice Forge fuzz test: يجرب قيم عشوائية لـ amount
    function testFuzz_Withdraw(uint96 amount) public {{
        vm.assume(amount > 0 && amount <= 5 ether);
        bank.deposit{{value: amount}}();
        uint256 before = address(bank).balance;
        bank.withdraw();
        // Invariant: رصيد البنك لا يرتفع بعد السحب
        assertLe(address(bank).balance, before, "Invariant violated: balance increased after withdraw");
    }}

    /// @notice اختبار ثابت لثغرة الـ Reentrancy
    function test_Reentrancy() public {{
        uint256 bankBefore = address(bank).balance;
        attacker.attack{{value: {amount_range.replace("ether", " ether") if "max" not in amount_range else "1 ether"}}}();
        // إذا سرق المهاجم أكثر مما أودع → bug
        uint256 stolen = bankBefore - address(bank).balance;
        uint256 deposited = 1 ether; // ما أودعه المهاجم
        if (stolen > deposited) {{
            emit log_named_uint("REENTRANCY BUG: stolen", stolen);
            emit log_named_uint("deposited", deposited);
            assertTrue(false, "Reentrancy: attacker drained more than deposited");
        }}
    }}
}}
""")
    return test_file


# ─────────────────────────────────────────────
# تحليل نتيجة forge
# ─────────────────────────────────────────────
def analyze_result(returncode: int, stdout: str, stderr: str) -> dict:
    """يحلل مخرجات forge ويصنف النتيجة."""
    output = stdout + stderr
    bugs = []

    # أنماط الـ crashes/bugs
    patterns = {
        "Reentrancy":        "reentrancy",
        "Overflow":          ["overflow", "arithmetic"],
        "Assertion Failed":  ["assertion failed", "assertTrue(false", "invariant violated"],
        "Panic":             "panic",
        "Revert":            "revert",
        "OutOfGas":          "out of gas",
        "StorageCollision":  "storage collision",
    }

    for bug_type, keywords in patterns.items():
        if isinstance(keywords, str):
            keywords = [keywords]
        for kw in keywords:
            if kw.lower() in output.lower():
                bugs.append(bug_type)
                break

    is_crash = returncode != 0 and len(bugs) > 0
    return {
        "returncode": returncode,
        "bugs":       list(set(bugs)),
        "is_crash":   is_crash,
        "stdout":     stdout[:3000],
        "stderr":     stderr[:2000],
    }


# ─────────────────────────────────────────────
# حفظ الـ PoC
# ─────────────────────────────────────────────
def save_poc(iteration: int, test_file: Path, result: dict) -> Path:
    """يحفظ ملف PoC في مجلد poc/."""
    POC_DIR.mkdir(exist_ok=True)
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    poc = POC_DIR / f"poc_{ts}_iter{iteration}.txt"

    content = f"""# PoC — Iteration {iteration}
# Timestamp: {ts}
# Bugs Found: {', '.join(result['bugs'])}
# Return Code: {result['returncode']}

## Test File
{test_file.read_text()}

## forge Output (stdout)
{result['stdout']}

## forge Output (stderr)
{result['stderr']}

## Artifact Link
{ARTIFACT_LINK}
"""
    poc.write_text(content)
    return poc


# ─────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────
def main() -> None:
    start_time = time.time()
    report     = {"runs": [], "total_bugs": 0, "start": datetime.utcnow().isoformat()}

    print(f"[START] Fuzz Agent — {datetime.utcnow().isoformat()}")
    print(f"[INFO]  Max iterations: {MAX_ITERATIONS} | Time limit: {TIME_LIMIT_SEC}s")

    # كتابة العقود المستهدفة
    write_fuzz_contracts()

    # إرسال إشعار بدء
    send_to_telegram(
        f"🚀 *Fuzz Agent Started*\n"
        f"Repo: `{GITHUB_REPO}`\n"
        f"Run ID: `{GITHUB_RUN_ID}`\n"
        f"Max Iterations: {MAX_ITERATIONS}\n"
        f"[View Run]({ARTIFACT_LINK})"
    )

    bugs_found = 0

    for i in range(1, MAX_ITERATIONS + 1):
        elapsed = time.time() - start_time
        if elapsed > TIME_LIMIT_SEC:
            print(f"[INFO] Time limit reached after {elapsed:.0f}s — stopping.")
            break

        seed       = random.randint(0, 2**32)
        test_file  = generate_fuzz_test(i, seed)

        print(f"\n[ITER {i:03d}/{MAX_ITERATIONS}] seed={seed} elapsed={elapsed:.0f}s")

        try:
            proc = subprocess.run(
                ["forge", "test",
                 "--match-path", str(test_file),
                 "--fuzz-runs", "256",      # عدد runs داخل كل fuzz test
                 "-v"],                     # verbose
                capture_output=True,
                text=True,
                timeout=120,               # max 2 دقيقة لكل iteration
            )
        except subprocess.TimeoutExpired:
            print(f"[WARN] Timeout في iteration {i}")
            continue
        except FileNotFoundError:
            print("[ERROR] forge غير موجود — تحقق من تثبيت Foundry.")
            break
        except Exception as e:
            print(f"[ERROR] {e}")
            continue

        result = analyze_result(proc.returncode, proc.stdout, proc.stderr)
        report["runs"].append({"iteration": i, "seed": seed, **result})

        if result["is_crash"]:
            bugs_found += 1
            report["total_bugs"] += 1
            poc_file = save_poc(i, test_file, result)

            bug_types = ", ".join(result["bugs"]) or "Unknown"
            msg = (
                f"🐛 *New Bug Found in Fuzzing!*\n\n"
                f"*Type:* `{bug_types}`\n"
                f"*Iteration:* {i}/{MAX_ITERATIONS}\n"
                f"*Seed:* `{seed}`\n"
                f"*Repo:* `{GITHUB_REPO}`\n"
                f"*Run:* [View Artifacts]({ARTIFACT_LINK})\n\n"
                f"*forge output (tail):*\n```\n{proc.stdout[-600:]}\n```"
            )
            send_to_telegram(msg, poc_file=poc_file)
            print(f"[BUG] Found: {bug_types}")
        else:
            status = "✅ PASS" if proc.returncode == 0 else "⚠️ FAIL (no known bug)"
            print(f"[ITER {i:03d}] {status}")

        # نظف ملفات الاختبار القديمة لتوفير مساحة
        if i > 5:
            old = Path("test") / f"FuzzRound_{i-5}.t.sol"
            old.unlink(missing_ok=True)

    # ─────────────────────────────────
    # ملخص نهائي
    # ─────────────────────────────────
    report["end"]         = datetime.utcnow().isoformat()
    report["total_iters"] = i
    REPORT_FILE.write_text(json.dumps(report, indent=2))

    summary = (
        f"✅ *Fuzz Agent Finished*\n\n"
        f"*Repo:* `{GITHUB_REPO}`\n"
        f"*Iterations:* {i}/{MAX_ITERATIONS}\n"
        f"*Bugs Found:* {bugs_found}\n"
        f"*Duration:* {(time.time()-start_time)/60:.1f} min\n"
        f"[View Artifacts]({ARTIFACT_LINK})"
    )
    send_to_telegram(summary)
    print(f"\n[DONE] Bugs: {bugs_found} | Iterations: {i}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        print(f"[FATAL] {err}")
        send_to_telegram(f"💥 *Fuzz Agent Crashed!*\n```\n{err[:500]}\n```")
        raise
