```python
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

import sys
import boto3
import botocore

def restore_folder(bucket_name, prefix, restore_days=30, 
                   aws_access_key_id=None, aws_secret_access_key=None):
    """
    Recursively restores all Glacier/Deep Archive objects under the given prefix in an S3 bucket
    using the Bulk retrieval tier (cheapest option). The restored copy remains in S3 Standard
    for 'restore_days' days, then expires automatically.

    Parameters
    ----------
    bucket_name : str
        The name of the S3 bucket to restore from.
    prefix : str
        The folder prefix (e.g., 'my-folder/') to restore recursively.
    restore_days : int, optional
        Number of days to keep the restored data in S3 Standard (default 30).
    aws_access_key_id : str, optional
        AWS Access Key ID if you want to override the default AWS credential chain.
    aws_secret_access_key : str, optional
        AWS Secret Access Key if you want to override the default AWS credential chain.

    Returns
    -------
    None
        Prints status messages and summary of the restore operation.
    """
    # Hardcode the retrieval tier to "Bulk" for cost minimization
    retrieval_tier = "Bulk"

    # Create the S3 client. If credentials are not provided, fallback to the default chain
    s3_client = boto3.client(
        "s3",
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key
    )

    # Use the paginator to list all objects matching the prefix
    paginator = s3_client.get_paginator("list_objects_v2")
    page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)

    # Track counters for various outcomes
    restored_count = 0
    already_in_progress_count = 0
    not_glacier_count = 0
    error_count = 0

    for page in page_iterator:
        # If there's no "Contents" in this page, skip
        if "Contents" not in page:
            continue

        # Go through each object in the current page
        for obj in page["Contents"]:
            key = obj["Key"]
            # If StorageClass not present, assume 'STANDARD'
            storage_class = obj.get("StorageClass", "STANDARD")
            restore_status = obj.get("Restore")  # 'None' if not being restored / no restore has happened

            # Only attempt to restore if object is in Glacier / Deep Archive / Glacier IR
            if storage_class in ["GLACIER", "DEEP_ARCHIVE", "GLACIER_IR"]:
                # If there's no active or completed restore
                if not restore_status:
                    try:
                        # Initiate the restore request
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
                else:
                    # Already in progress or restored
                    already_in_progress_count += 1
                    print(f"[SKIPPED] {key} is already in restore process or restored.")
            else:
                # Not in Glacier or Deep Archive
                not_glacier_count += 1
                print(f"[SKIPPED] {key} is not in Glacier/Deep Archive (StorageClass: {storage_class}).")

    # Print a summary of the operation
    print("\n--- Restore Summary ---")
    print(f"Total initiated restores: {restored_count}")
    print(f"Already in progress/restored: {already_in_progress_count}")
    print(f"Skipped (not Glacier/Deep Archive): {not_glacier_count}")
    print(f"Errors: {error_count}")


def main():
    """
    Prompts the user for S3 bucket name, prefix, restore days, and optional credentials,
    then runs the restore_folder function to initiate Bulk-tier temporary restores.
    """
    print("=== S3 Glacier TEMPORARY Bulk Restore Script ===")
    # Collect user inputs
    bucket_name = input("Enter S3 bucket name: ").strip()
    prefix = input("Enter folder prefix (e.g., 'my-folder/'): ").strip()

    # Ask for how many days the temporary restore should remain
    restore_days_input = input("Enter the number of days to keep the restored copy (default 30): ").strip()
    restore_days = int(restore_days_input) if restore_days_input else 30

    print("\nIf you leave the following blank, the default AWS credential chain will be used.")
    aws_access_key_id = input("Enter AWS Access Key ID (or leave blank): ").strip()
    aws_secret_access_key = input("Enter AWS Secret Access Key (or leave blank): ").strip()

    # Convert empty strings to None for Boto3
    if not aws_access_key_id:
        aws_access_key_id = None
    if not aws_secret_access_key:
        aws_secret_access_key = None

    # Test bucket accessibility
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

    # Show a small sample of objects under the prefix
    print("\nChecking objects under the prefix...\n")
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
        print(f"[WARNING] No objects found under prefix '{prefix}' in bucket '{bucket_name}'.")
    else:
        print(f"Found {object_count} object(s) under prefix '{prefix}'. Showing up to 5 sample keys:")
        for i, key in enumerate(sample_objects, start=1):
            print(f"  {i}. {key}")

    # Display the final configuration for confirmation
    print("\n--- Configuration ---")
    print(f"Bucket: {bucket_name}")
    print(f"Prefix: {prefix}")
    print("Retrieval Tier: Bulk (Forced, to minimize cost)")
    print(f"Restore Days (TEMPORARY): {restore_days}")
    if aws_access_key_id and aws_secret_access_key:
        print(f"AWS Access Key ID: {aws_access_key_id}")
        print(f"AWS Secret Access Key: {'*' * len(aws_secret_access_key)}")
    else:
        print("No explicit AWS credentials provided; using default credential chain.")

    # Final user confirmation
    proceed = input("\nDo you want to proceed with the TEMPORARY Bulk restore? (y/N): ").strip().lower()
    if proceed != 'y':
        print("Exiting without restoring objects.")
        sys.exit(0)

    # Initiate the restore
    print("\nStarting temporary Bulk restore process...\n")
    restore_folder(
        bucket_name=bucket_name,
        prefix=prefix,
        restore_days=restore_days,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key
    )

    print("\nRestore process completed.")


if __name__ == "__main__":
    main()
```
