"""
fuzz_agent.py — Smart Contract Fuzzing Agent (نسخة مُصححة)
دقة اكتشاف محسّنة: يعتمد على [FAIL] الفعلي من forge
وليس مجرد وجود كلمة في النص.
"""

import os
import re
import json
import random
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# إعدادات
# ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GITHUB_REPO        = os.environ.get("GITHUB_REPOSITORY", "unknown/repo")
GITHUB_RUN_ID      = os.environ.get("GITHUB_RUN_ID", "0")
GITHUB_SERVER_URL  = os.environ.get("GITHUB_SERVER_URL", "https://github.com")

ARTIFACT_LINK = f"{GITHUB_SERVER_URL}/{GITHUB_REPO}/actions/runs/{GITHUB_RUN_ID}"

MAX_ITERATIONS = 80
TIME_LIMIT_SEC = 18000
POC_DIR        = Path("poc")
REPORT_FILE    = Path("fuzz_report.json")


# ─────────────────────────────────────────────
# Telegram
# ─────────────────────────────────────────────
def send_to_telegram(message: str, poc_file: Path | None = None) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[WARN] Telegram secrets غير موجودة.")
        return
    base_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
    try:
        resp = requests.post(
            f"{base_url}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        if poc_file and poc_file.exists():
            with open(poc_file, "rb") as f:
                requests.post(
                    f"{base_url}/sendDocument",
                    data={"chat_id": TELEGRAM_CHAT_ID},
                    files={"document": (poc_file.name, f, "text/plain")},
                    timeout=30,
                ).raise_for_status()
    except Exception as e:
        print(f"[ERROR] Telegram: {e}")


# ─────────────────────────────────────────────
# كتابة العقود المستهدفة
# ─────────────────────────────────────────────
def write_fuzz_contracts() -> None:
    src = Path("src")
    src.mkdir(exist_ok=True)

    (src / "VulnerableBank.sol").write_text("""\
// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract VulnerableBank {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    // ⚠️ Reentrancy: state update AFTER external call
    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "No balance");
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "Transfer failed");
        balances[msg.sender] = 0;
    }

    receive() external payable {}
}
""")
    print("[INFO] تم كتابة العقود.")


# ─────────────────────────────────────────────
# توليد اختبار Fuzz لكل iteration
# ─────────────────────────────────────────────
def generate_fuzz_test(iteration: int, seed: int) -> Path:
    test_dir = Path("test")
    test_dir.mkdir(exist_ok=True)

    random.seed(seed)
    attack_depth   = random.randint(1, 5)
    deposit_amount = random.choice([
        "0.1 ether", "0.5 ether", "1 ether", "2 ether", "5 ether"
    ])
    use_reentrancy_attacker = random.random() > 0.4

    test_file = test_dir / f"FuzzRound_{iteration}.t.sol"

    if use_reentrancy_attacker:
        attacker_code = f"""
contract ReentrancyAttacker {{
    VulnerableBank private bank;
    uint8 private depth;

    constructor(address _bank) {{ bank = VulnerableBank(payable(_bank)); }}

    receive() external payable {{
        if (depth < {attack_depth} && address(bank).balance >= 0.1 ether) {{
            depth++;
            bank.withdraw();
        }}
    }}

    function attack() external payable {{
        require(msg.value >= 0.1 ether, "Need ETH");
        bank.deposit{{value: msg.value}}();
        bank.withdraw();
    }}
}}"""
        test_body = f"""
    VulnerableBank     bank;
    ReentrancyAttacker attacker;

    function setUp() public {{
        bank     = new VulnerableBank();
        attacker = new ReentrancyAttacker(address(bank));
        vm.deal(address(bank), 10 ether);
        vm.deal(address(attacker), {deposit_amount});
    }}

    function test_ReentrancyDrain() public {{
        uint256 bankBefore     = address(bank).balance;
        uint256 deposited      = {deposit_amount};

        attacker.attack{{value: deposited}}();

        uint256 bankAfter      = address(bank).balance;
        uint256 drainedFromBank = bankBefore > bankAfter ? bankBefore - bankAfter : 0;

        // ✅ Invariant صحيح: ما سُرق لا يتجاوز ما أُودع
        assertLe(
            drainedFromBank,
            deposited,
            "REENTRANCY: attacker drained more than deposited!"
        );
    }}

    function testFuzz_WithdrawAmount(uint96 amount) public {{
        vm.assume(amount > 0.001 ether && amount <= 5 ether);
        vm.deal(address(this), amount);
        bank.deposit{{value: amount}}();
        uint256 bankBefore = address(bank).balance;
        bank.withdraw();
        assertEq(address(bank).balance, bankBefore - amount, "Withdraw amount mismatch");
    }}

    receive() external payable {{}}"""
    else:
        attacker_code = ""
        test_body = f"""
    VulnerableBank bank;

    function setUp() public {{
        bank = new VulnerableBank();
        vm.deal(address(this), 10 ether);
    }}

    function testFuzz_NormalWithdraw(uint96 amount) public {{
        vm.assume(amount > 0.001 ether && amount <= 5 ether);
        bank.deposit{{value: amount}}();
        uint256 before = address(bank).balance;
        bank.withdraw();
        assertEq(address(bank).balance, before - amount, "Balance mismatch");
    }}

    receive() external payable {{}}"""

    test_file.write_text(f"""\
// SPDX-License-Identifier: MIT
// Auto-generated — Iteration {iteration} — Seed {seed}
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "../src/VulnerableBank.sol";

{attacker_code}

contract FuzzRound_{iteration} is Test {{
{test_body}
}}
""")
    return test_file


# ─────────────────────────────────────────────
# ✅ تحليل دقيق — يعتمد على [FAIL] الفعلي
# ─────────────────────────────────────────────
def analyze_result(returncode: int, stdout: str, stderr: str) -> dict:
    """
    forge يطبع:
      [PASS]  عند نجاح الـ test  → ليس bug
      [FAIL.  عند فشل الـ test   → bug حقيقي
    نبحث عن [FAIL فقط وليس عن كلمات عشوائية.
    """
    output = stdout + stderr

    # ── هل فشل test فعلاً؟ ──
    forge_failed  = bool(re.search(r"\[FAIL", output))
    compile_error = bool(re.search(
        r"(compiler error|error\[E|solc error|compilation failed)",
        output, re.IGNORECASE
    ))

    bugs = []
    counterexample = ""

    if forge_failed and not compile_error:
        # استخرج سياق الفشل فقط
        fail_blocks = re.findall(r"\[FAIL[^\n]*\n(?:[^\n]*\n){0,5}", output)
        fail_ctx    = " ".join(fail_blocks).lower()

        rules = {
            "Reentrancy":       ["drained more than deposited", "reentrancy"],
            "Integer Overflow":  ["overflow", "arithmetic"],
            "Assertion Failed":  ["assertle failed", "asserteq failed", "assertgt failed"],
            "Invariant Broken":  ["invariant", "balance mismatch", "withdraw amount mismatch"],
            "Panic":             ["panic"],
            "Revert":            ["revert"],
            "OutOfGas":          ["out of gas"],
        }
        for bug_type, kws in rules.items():
            if any(kw in fail_ctx for kw in kws):
                bugs.append(bug_type)

        if not bugs:
            bugs.append("Unknown Test Failure")

        # استخرج counterexample
        ce_match = re.search(r"Counterexample:.*?(?=\n\n|\Z)", output, re.DOTALL)
        counterexample = ce_match.group(0).strip() if ce_match else ""

    # forge يطبع [FAIL مرتين: في النتيجة وفي "Failing tests:" section
    # نعتمد على السطر الملخص: "N tests passed, M failed"
    summary_match = re.search(r"(\d+) tests passed, (\d+) failed", output)
    if summary_match:
        passed_count = int(summary_match.group(1))
        failed_count = int(summary_match.group(2))
    else:
        failed_count = len(re.findall(r"^\[FAIL", output, re.MULTILINE))
        passed_count = len(re.findall(r"^\[PASS\]", output, re.MULTILINE))

    return {
        "returncode":     returncode,
        "forge_failed":   forge_failed,
        "compile_error":  compile_error,
        "is_bug":         forge_failed and not compile_error,
        "bugs":           list(set(bugs)),
        "failed_tests":   failed_count,
        "passed_tests":   passed_count,
        "counterexample": counterexample,
        "stdout":         stdout[:3000],
        "stderr":         stderr[:1000],
    }


# ─────────────────────────────────────────────
# حفظ PoC
# ─────────────────────────────────────────────
def save_poc(iteration: int, test_file: Path, result: dict) -> Path:
    POC_DIR.mkdir(exist_ok=True)
    ts  = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    poc = POC_DIR / f"poc_{ts}_iter{iteration}.txt"
    poc.write_text(f"""# PoC Report — Iteration {iteration}
# Timestamp     : {ts}
# Bug Types     : {', '.join(result['bugs'])}
# Failed Tests  : {result['failed_tests']}
# Passed Tests  : {result['passed_tests']}
# Counterexample: {result['counterexample']}

## Artifact Link
{ARTIFACT_LINK}

## Test File
{test_file.read_text()}

## forge stdout
{result['stdout']}

## forge stderr
{result['stderr']}
""")
    return poc


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
def main() -> None:
    start_time = time.time()
    report     = {"runs": [], "total_bugs": 0, "start": datetime.utcnow().isoformat()}

    print(f"[START] Fuzz Agent — {datetime.utcnow().isoformat()}")
    print(f"[INFO]  Max iterations: {MAX_ITERATIONS} | Time limit: {TIME_LIMIT_SEC}s")

    write_fuzz_contracts()
    send_to_telegram(
        f"🚀 *Fuzz Agent Started*\n"
        f"Repo: `{GITHUB_REPO}`\n"
        f"Run: [View]({ARTIFACT_LINK})"
    )

    bugs_found = 0

    for i in range(1, MAX_ITERATIONS + 1):
        if time.time() - start_time > TIME_LIMIT_SEC:
            print("[INFO] Time limit — stopping.")
            break

        seed      = random.randint(0, 2**32)
        test_file = generate_fuzz_test(i, seed)
        elapsed   = time.time() - start_time

        print(f"\n[ITER {i:03d}/{MAX_ITERATIONS}] seed={seed} elapsed={elapsed:.0f}s")

        try:
            proc = subprocess.run(
                ["forge", "test",
                 "--match-path", str(test_file),
                 "--fuzz-runs", "512",
                 "-vv"],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            print(f"[WARN] Timeout iter {i}")
            continue
        except FileNotFoundError:
            print("[ERROR] forge not found")
            break

        result = analyze_result(proc.returncode, proc.stdout, proc.stderr)

        report["runs"].append({
            "iteration":    i,
            "seed":         seed,
            "is_bug":       result["is_bug"],
            "bugs":         result["bugs"],
            "passed_tests": result["passed_tests"],
            "failed_tests": result["failed_tests"],
        })

        if result["compile_error"]:
            print(f"[ITER {i:03d}] ⚙️  Compile Error (not a bug)")

        elif result["is_bug"]:
            bugs_found += 1
            report["total_bugs"] += 1
            poc_file  = save_poc(i, test_file, result)
            bug_types = ", ".join(result["bugs"])
            ce_info   = (f"\n*Counterexample:*\n`{result['counterexample'][:200]}`"
                         if result["counterexample"] else "")

            msg = (
                f"🐛 *New Bug Found!*\n\n"
                f"*Type:* `{bug_types}`\n"
                f"*Iteration:* {i}/{MAX_ITERATIONS}\n"
                f"*Seed:* `{seed}`\n"
                f"*Failed Tests:* {result['failed_tests']}\n"
                f"*Passed Tests:* {result['passed_tests']}\n"
                f"{ce_info}\n"
                f"*Repo:* `{GITHUB_REPO}`\n"
                f"[View Artifacts]({ARTIFACT_LINK})"
            )
            send_to_telegram(msg, poc_file=poc_file)
            print(f"[BUG ✓] {bug_types} | failed={result['failed_tests']} passed={result['passed_tests']}")

        else:
            print(f"[ITER {i:03d}] ✅ PASS ({result['passed_tests']} tests)")

        if i > 5:
            (Path("test") / f"FuzzRound_{i-5}.t.sol").unlink(missing_ok=True)

    report["end"] = datetime.utcnow().isoformat()
    REPORT_FILE.write_text(json.dumps(report, indent=2))

    send_to_telegram(
        f"✅ *Fuzz Agent Finished*\n"
        f"*Iterations:* {i}/{MAX_ITERATIONS}\n"
        f"*Real Bugs:* {bugs_found}\n"
        f"*Duration:* {(time.time()-start_time)/60:.1f} min\n"
        f"[Artifacts]({ARTIFACT_LINK})"
    )
    print(f"\n[DONE] Real Bugs: {bugs_found} | Iterations: {i}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        err = traceback.format_exc()
        print(f"[FATAL] {err}")
        send_to_telegram(f"💥 *Agent Crashed*\n```\n{err[:500]}\n```")
        raise
