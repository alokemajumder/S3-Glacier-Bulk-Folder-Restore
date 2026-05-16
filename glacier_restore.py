#!/usr/bin/env python3
"""
S3 Glacier Bulk Folder Restore (Temporary Access)

Recursively restores all objects stored in Amazon S3 Glacier or Deep Archive 
under a specified prefix (folder). Uses the Bulk retrieval tier to minimize costs, 
and restores data temporarily for a user-defined number of days.

Written by: Aloke Majumder (@alokemajumder)
Disclaimer: Use at your own risk. This script is provided "as is," 
without any warranties or guarantees of any kind. Always verify costs 
and configurations before running bulk restore operations in production.

Usage:
  1. Run this script.
  2. Enter the bucket name, prefix, restore duration, and optional AWS credentials when prompted.
  3. Confirm the restore operation after reviewing a sample of objects.
  4. The script initiates a Bulk-tier temporary restore for any Glacier/Deep Archive objects.

After the chosen restore period ('restore_days'), the temporary S3 Standard copy expires.
If you need the data permanently in S3 Standard, copy it to another location 
or set a lifecycle rule after restoration.
"""

# NOTES:
# 1. Recursive Listing:
#    By specifying a prefix and using list_objects_v2 with pagination, 
#    this script automatically captures all matching keys (including subfolders).
# 
# 2. Bulk Tier:
#    The "Bulk" retrieval tier is forced to minimize costs (but it's slower than Standard or Expedited).
# 
# 3. Temporary Restore:
#    Objects remain in their original storage class; a temporary S3 Standard copy is created
#    for the specified number of days, then it expires.
# 
# 4. Storage Classes:
#    We check for "GLACIER", "DEEP_ARCHIVE", or "GLACIER_IR". Objects in other classes are skipped.

# S3 Glacier Bulk Folder Restore (Temporary Access) with Skip List + Dry Run
# ADDED support for a skip list of folder names and certain files.

# Adds:
# - Skip list file support (folders + filenames)
#  - Dry run mode (no restore calls)

import sys
import boto3
import botocore


# -------------------------------
# Skip list helpers
# -------------------------------

def load_skip_list(skip_file):
    """Load skip items from a text file."""
    skip_items = []
    try:
        with open(skip_file, "r") as f:
            for line in f:
                item = line.strip()
                if item and not item.startswith("#"):
                    skip_items.append(item)
    except FileNotFoundError:
        print(f"[WARNING] Skip file '{skip_file}' not found. No items will be skipped.")
    return skip_items


def should_skip_key(key, skip_items):
    """Return True if the S3 key should be skipped based on folder or filename rules."""
    parts = key.split("/")  # split into path components

    for item in skip_items:
        # Skip if any folder component contains the skip item
        if any(item in part for part in parts[:-1]):  # folders only
            return True

        # Skip if filename ends with the skip item
        if parts[-1].endswith(item):
            return True

    return False



# -------------------------------
# Restore logic
# -------------------------------

def restore_folder(bucket_name, prefix, restore_days=30,
                   aws_access_key_id=None, aws_secret_access_key=None,
                   skip_items=None, dry_run=False):

    # Skip entire prefix if it contains any skip term
    if skip_items:
        for item in skip_items:
            if item in prefix:
                # print(f"[SKIPPED PREFIX] Prefix '{prefix}' contains skip term '{item}'.")
                return

    """
    Recursively restores all Glacier/Deep Archive objects under the given prefix.
    Supports skip list and dry run mode.
    """

    retrieval_tier = "Bulk"

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key
    )

    paginator = s3_client.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    restored_count = 0
    already_in_progress_count = 0
    not_glacier_count = 0
    skipped_filter_count = 0
    error_count = 0

    for page in page_iterator:
        if "Contents" not in page:
            continue

        for obj in page["Contents"]:
            key = obj["Key"]
            storage_class = obj.get("StorageClass", "STANDARD")
            restore_status = obj.get("Restore")

            # Skip unwanted folders/files
            if skip_items and should_skip_key(key, skip_items):
                # print(f"[SKIPPED - FILTER] {key}")
                skipped_filter_count += 1
                continue

            # Only restore Glacier/Deep Archive
            if storage_class not in ["GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"]:
                if not dry_run:
                    print(f"[SKIPPED] {key} is not in Glacier/Deep Archive (StorageClass: {storage_class}).")
                not_glacier_count += 1
                continue

            # If already restored or in progress
            if restore_status:
                if not dry_run:
                    print(f"[SKIPPED] {key} is already in restore process or restored.")
                already_in_progress_count += 1
                continue

            # DRY RUN MODE
            if dry_run:
                print(f"[DRY RUN] Would restore: {key}")
                restored_count += 1
                continue

            # REAL RESTORE CALL
            try:
                s3_client.restore_object(
                    Bucket=bucket_name,
                    Key=key,
                    RestoreRequest={
                        "Days": restore_days,
                        "GlacierJobParameters": {
                            "Tier": retrieval_tier
                        }
                    }
                )
                restored_count += 1
                print(f"[INITIATED] Bulk restore request for: {key}")

            except botocore.exceptions.ClientError as e:
                error_count += 1
                print(f"[ERROR] Failed to restore {key}: {e}")

    # Summary
    print("\n--- Restore Summary ---")
    print(f"Total restore actions (or dry-run actions): {restored_count}")
    print(f"Already in progress/restored: {already_in_progress_count}")
    print(f"Skipped (not Glacier/Deep Archive): {not_glacier_count}")
    print(f"Skipped by filter: {skipped_filter_count}")
    print(f"Errors: {error_count}")


# -------------------------------
# Main program
# -------------------------------

def main():
    print("=== S3 Glacier TEMPORARY Bulk Restore Script (with Skip List + Dry Run) ===")

    bucket_name = input("Enter S3 bucket name: ").strip()
    prefix = input("Enter folder prefix (e.g., 'my-folder/'): ").strip()

    restore_days_input = input("Enter number of days to keep restored copy (default 30): ").strip()
    restore_days = int(restore_days_input) if restore_days_input else 30

    print("\nIf left blank, default AWS credential chain will be used.")
    aws_access_key_id = input("Enter AWS Access Key ID (or leave blank): ").strip() or None
    aws_secret_access_key = input("Enter AWS Secret Access Key (or leave blank): ").strip() or None

    # Skip list file
    skip_file = input("Enter path to skip-list file (or leave blank): ").strip()
    skip_items = load_skip_list(skip_file) if skip_file else []

    # Dry run mode
    dry_run_input = input("Dry run only (no restore calls)? (y/N): ").strip().lower()
    dry_run = (dry_run_input == "y")

    # Test bucket access
    try:
        s3_client = boto3.client(
            "s3",
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key
        )
        s3_client.head_bucket(Bucket=bucket_name)
        print(f"\n[OK] Bucket '{bucket_name}' is accessible.")
    except botocore.exceptions.ClientError as e:
        print(f"[ERROR] Could not access bucket '{bucket_name}': {e}")
        sys.exit(1)

    # Show sample objects
    print("\nChecking objects under prefix...\n")
    paginator = s3_client.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    sample_objects = []
    object_count = 0
    for page in page_iterator:
        if "Contents" not in page:
            continue
        for obj in page["Contents"]:
            sample_objects.append(obj["Key"])
            object_count += 1
            if len(sample_objects) >= 5:
                break
        if len(sample_objects) >= 5:
            break

    if object_count == 0:
        print(f"[WARNING] No objects found under prefix '{prefix}'.")
    else:
        print(f"Found {object_count} object(s). Showing up to 5 sample keys:")
        for i, key in enumerate(sample_objects, start=1):
            print(f"  {i}. {key}")

    # Confirm
    print("\n--- Configuration ---")
    print(f"Bucket: {bucket_name}")
    print(f"Prefix: {prefix}")
    print(f"Restore Days: {restore_days}")
    print(f"Skip items: {skip_items if skip_items else 'None'}")
    print(f"Dry run: {dry_run}")

    proceed = input("\nProceed with restore? (y/N): ").strip().lower()
    if proceed != "y":
        print("Exiting without restoring.")
        sys.exit(0)

    print("\nStarting restore process...\n")
    restore_folder(
        bucket_name=bucket_name,
        prefix=prefix,
        restore_days=restore_days,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        skip_items=skip_items,
        dry_run=dry_run
    )

    print("\nRestore process completed.")


if __name__ == "__main__":
    main()
