#!/usr/bin/env python3
"""
Quick launcher for both pipelines with clear options.

Usage:
  python scripts/run_pipeline_launcher.py [linear|iterative] [options]

Examples:
  python scripts/run_pipeline_launcher.py linear
  python scripts/run_pipeline_launcher.py iterative --max-test-retries 5
  python scripts/run_pipeline_launcher.py iterative --input my_code.py --output-dir ./results
"""

import sys
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def print_menu():
    """Print pipeline selection menu."""
    print("\n" + "="*70)
    print("MIGRATION PIPELINE LAUNCHER")
    print("="*70)
    print("\n1. LINEAR PIPELINE (fast, no automatic retries)")
    print("   migration → test → review → output")
    print("   Use for: Quick prototyping, testing setup")
    print("\n2. ITERATIVE PIPELINE (robust, auto-improvement with feedback loops)")
    print("   migration ↔ test ↔ review (up to 3 retries each)")
    print("   Use for: Production migrations, critical code")
    print("\n3. EXIT")
    print("\n" + "="*70)


def run_linear():
    """Run linear pipeline."""
    print("\n[LINEAR PIPELINE]")
    print("Command: python scripts/run_pipeline_real.py")
    print("Output: ./.run_output/\n")
    
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", help="Input Python file (default: url.py)")
    parser.add_argument("--examples", type=int, default=10, help="Training examples")
    parser.add_argument("--timeout", type=int, default=300, help="Test timeout (sec)")
    args, unknown = parser.parse_known_args()
    
    cmd = ["python", str(REPO_ROOT / "scripts/run_pipeline_real.py")]
    if args.input:
        cmd.extend(["--input", args.input])
    if args.examples != 10:
        cmd.extend(["--examples", str(args.examples)])
    if args.timeout != 300:
        cmd.extend(["--timeout", str(args.timeout)])
    
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    return result.returncode


def run_iterative():
    """Run iterative pipeline."""
    print("\n[ITERATIVE PIPELINE]")
    print("Command: python scripts/run_pipeline_with_feedback.py")
    print("Output: ./.run_output_iterative/\n")
    
    import argparse
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--input", help="Input Python file (default: url.py)")
    parser.add_argument("--output-dir", default="./.run_output_iterative", help="Output dir")
    parser.add_argument("--max-test-retries", type=int, default=3, help="Max test retries")
    parser.add_argument("--max-review-retries", type=int, default=3, help="Max review retries")
    args, unknown = parser.parse_known_args()
    
    cmd = ["python", str(REPO_ROOT / "scripts/run_pipeline_with_feedback.py")]
    if args.input:
        cmd.extend(["--input", args.input])
    if args.output_dir != "./.run_output_iterative":
        cmd.extend(["--output-dir", args.output_dir])
    if args.max_test_retries != 3:
        cmd.extend(["--max-test-retries", str(args.max_test_retries)])
    if args.max_review_retries != 3:
        cmd.extend(["--max-review-retries", str(args.max_review_retries)])
    
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    return result.returncode


def main():
    """Main launcher."""
    if len(sys.argv) > 1:
        choice = sys.argv[1].lower()
        # Remove choice from sys.argv so subparsers get clean args
        sys.argv.pop(1)
    else:
        print_menu()
        choice = input("\nSelect pipeline (1/2/3): ").strip()
    
    if choice in ["1", "linear"]:
        return run_linear()
    elif choice in ["2", "iterative"]:
        return run_iterative()
    elif choice in ["3", "exit"]:
        print("\nExiting.")
        return 0
    else:
        print(f"\nInvalid choice: {choice}")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
