"""
tools/kb_setup.py
=================
Create the three Bedrock Knowledge Bases (Task 5) over the S3 vector indexes
that the CloudFormation stack already made, ingest the policy documents, and
write the resulting KB IDs into .env.

Usage:  ./withaws.sh python tools/kb_setup.py [--dry]
"""
import argparse
import os
import re
import sys
import time

import boto3

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import config  # noqa: E402

REGION = config.AWS_REGION
ACCOUNT = config._account_id() if hasattr(config, '_account_id') else \
    boto3.client('sts', region_name=REGION).get_caller_identity()['Account']
EMBEDDING_ARN = f'arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0'

DOMAINS = [
    ('returns',  'RETURNS_KB_ID',  'RETURNS_VECTOR_INDEX'),
    ('shipping', 'SHIPPING_KB_ID', 'SHIPPING_VECTOR_INDEX'),
    ('warranty', 'WARRANTY_KB_ID', 'WARRANTY_VECTOR_INDEX'),
]

agent = boto3.client('bedrock-agent', region_name=REGION)


def wait_kb_active(kb_id: str, timeout: int = 600) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        kb = agent.get_knowledge_base(knowledgeBaseId=kb_id)['knowledgeBase']
        status = kb['status']
        if status in ('ACTIVE', 'FAILED'):
            return status
        time.sleep(5)
    return 'TIMEOUT'


def wait_ingestion(kb_id: str, ds_id: str, job_id: str, timeout: int = 1800) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )['ingestionJob']
        status = job['status']
        if status in ('COMPLETE', 'FAILED'):
            return status, job
        time.sleep(10)
    return 'TIMEOUT', {}


def find_by_name(kind: str, name: str, kb_id: str = ''):
    """Return the id of an existing KB or data source with this exact name."""
    if kind == 'kb':
        for row in agent.list_knowledge_bases(maxResults=100).get('knowledgeBaseSummaries', []):
            if row['name'] == name:
                return row['knowledgeBaseId']
        return ''
    for row in agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=100).get(
            'dataSourceSummaries', []):
        if row['name'] == name:
            return row['dataSourceId']
    return ''


def set_env_value(path: str, key: str, value: str) -> None:
    with open(path, encoding='utf-8') as fh:
        text = fh.read()
    text = re.sub(rf'^{key}=.*$', f'{key}={value}', text, flags=re.M)
    with open(path, 'w', encoding='utf-8') as fh:
        fh.write(text)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry', action='store_true', help='print the plan only')
    args = parser.parse_args()

    vector_bucket_arn = config.VECTOR_STORE_BUCKET_ARN
    policy_bucket_arn = f'arn:aws:s3:::{config.POLICY_BUCKET}'
    role_arn = config.AGENTCORE_ROLE_ARN
    print(f'Vector bucket : {vector_bucket_arn}')
    print(f'Policy bucket : {policy_bucket_arn}')
    print(f'KB role       : {role_arn}')

    created = {}
    for domain, env_key, index_key in DOMAINS:
        index_name = getattr(config, index_key)
        kb_name = f'novamart-{domain}-policy-kb'
        ds_name = f'novamart-{domain}-docs'
        print(f'\n=== {domain}  (index {index_name})')

        kb_id = find_by_name('kb', kb_name)
        if kb_id:
            print(f'  KB already exists: {kb_id}')
        else:
            if args.dry:
                print('  would create KB')
                continue
            kb_id = agent.create_knowledge_base(
                name=kb_name,
                description=f'NovaMart {domain} policy documents',
                roleArn=role_arn,
                knowledgeBaseConfiguration={
                    'type': 'VECTOR',
                    'vectorKnowledgeBaseConfiguration': {
                        'embeddingModelArn': EMBEDDING_ARN,
                        'embeddingModelConfiguration': {
                            'bedrockEmbeddingModelConfiguration': {
                                'dimensions': 1024,
                                'embeddingDataType': 'FLOAT32',
                            }
                        },
                    },
                },
                storageConfiguration={
                    'type': 'S3_VECTORS',
                    's3VectorsConfiguration': {
                        'vectorBucketArn': vector_bucket_arn,
                        'indexName': index_name,
                    },
                },
            )['knowledgeBase']['knowledgeBaseId']
            status = wait_kb_active(kb_id)
            print(f'  created KB {kb_id} -> {status}')
            if status != 'ACTIVE':
                print(f'  ERROR: KB not ACTIVE ({status})')
                return 1

        ds_id = find_by_name('ds', ds_name, kb_id)
        if ds_id:
            print(f'  data source already exists: {ds_id}')
        else:
            if args.dry:
                print('  would create data source')
                created[env_key] = kb_id
                continue
            ds_id = agent.create_data_source(
                knowledgeBaseId=kb_id,
                name=ds_name,
                description=f'policies/{domain}/ in the NovaMart policy bucket',
                dataSourceConfiguration={
                    'type': 'S3',
                    's3Configuration': {
                        'bucketArn': policy_bucket_arn,
                        'bucketOwnerAccountId': ACCOUNT,
                        'inclusionPrefixes': [f'policies/{domain}/'],
                    },
                },
            )['dataSource']['dataSourceId']
            print(f'  created data source {ds_id}')

        if args.dry:
            created[env_key] = kb_id
            continue

        jobs = agent.list_ingestion_jobs(knowledgeBaseId=kb_id, dataSourceId=ds_id).get(
            'ingestionJobSummaries', [])
        if any(j['status'] == 'COMPLETE' for j in jobs):
            print('  ingestion already COMPLETE')
        else:
            job_id = agent.start_ingestion_job(
                knowledgeBaseId=kb_id, dataSourceId=ds_id,
                description=f'initial {domain} policy ingest'
            )['ingestionJob']['ingestionJobId']
            print(f'  ingestion job {job_id} started; polling...')
            status, job = wait_ingestion(kb_id, ds_id, job_id)
            print(f'  ingestion -> {status}')
            if status != 'COMPLETE':
                print(f'  ERROR: {job.get("failureReason", job)}')
                return 1

        created[env_key] = kb_id

    if args.dry:
        print('\n(dry run, nothing written)')
        return 0

    env_path = os.path.join(ROOT, '.env')
    for env_key, kb_id in created.items():
        set_env_value(env_path, env_key, kb_id)
        print(f'.env {env_key}={kb_id}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
