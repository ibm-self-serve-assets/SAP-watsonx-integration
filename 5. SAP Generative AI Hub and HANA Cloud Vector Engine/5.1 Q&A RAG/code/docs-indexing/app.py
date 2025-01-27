import mimetypes
import os
import hashlib
import json
import warnings
from dotenv import load_dotenv
from bs4 import BeautifulSoup
from tqdm import tqdm
from langchain.text_splitter import RecursiveCharacterTextSplitter
import boto3
from botocore.exceptions import ClientError
import tika
from tika import parser
import re
import pandas as pd
from hdbcli import dbapi
import time
# Initialize Tika

tika.initVM()

# Suppress warnings
warnings.filterwarnings("ignore")

# Load environment variables
load_dotenv()


# Environment configuration
AICORE_CONFIG = {
    "AUTH_URL": os.environ.get("AICORE_AUTH_URL"),
    "CLIENT_ID": os.environ.get("AICORE_CLIENT_ID"),
    "CLIENT_SECRET": os.environ.get("AICORE_CLIENT_SECRET"),
    "RESOURCE_GROUP": os.environ.get("AICORE_RESOURCE_GROUP"),
    "BASE_URL": os.environ.get("AICORE_BASE_URL")
}

HANA_DB_CONFIG = {
    "HOST": os.environ.get("HANA_DB_HOST"),
    "USER": os.environ.get("HANA_DB_USER"),
    "PASSWORD": os.environ.get("HANA_DB_PASSWORD"),
    "TABLE_NAME": os.environ.get("HANA_DB_TABLE_NAME")
}

AWS_CONFIG = {
    "ACCESS_KEY_ID": os.environ.get("AWS_ACCESS_KEY_ID"),
    "SECRET_ACCESS_KEY": os.environ.get("AWS_SECRET_ACCESS_KEY"),
    "DEFAULT_REGION": os.environ.get("AWS_DEFAULT_REGION"),
    "BUCKET": os.environ.get("AWS_BUCKET"),
    "DOC_PATH_PREFIX": os.environ.get("AWS_DOC_PATH_PREFIX")
}

parameters = {
    "bucket": AWS_CONFIG["BUCKET"],
    "path_prefix": AWS_CONFIG["DOC_PATH_PREFIX"],
    "ingestion_chunk_size": 256,
    "ingestion_chunk_overlap": 128
}

text_embedding_model = os.environ.get("EMBEDDING_MODEL")

from ai_core_sdk.ai_core_v2_client import AICoreV2Client
from gen_ai_hub.proxy.native.openai import embeddings
# Create an AI API client instance
try:
    ai_core_client = AICoreV2Client(
        base_url=AICORE_CONFIG["BASE_URL"],
        auth_url=AICORE_CONFIG["AUTH_URL"],
        client_id=AICORE_CONFIG["CLIENT_ID"],
        client_secret=AICORE_CONFIG["CLIENT_SECRET"]
    )

    # Get the number of GitHub repositories connected to SAP AI Core
    response = ai_core_client.repositories.query()
    print(f"Connected GitHub repositories: {response.count}")
except Exception as e:
    print(f"Error initializing AI Core client: {e}")
    exit(1)

def parse_pdfs_from_s3_buffer(bucket_name, prefix=""):
    """
    Parses PDF files from S3 bucket using Tika.
    
    Args:
        bucket_name (str): S3 bucket name.
        prefix (str): S3 prefix filter.

    Returns:
        List of tuples with file key, content, and metadata.
    """
    try:
        s3 = boto3.client('s3')
        paginator = s3.get_paginator('list_objects_v2')
        page_iterator = paginator.paginate(Bucket=bucket_name, Prefix=prefix)
        all_pdf_data = []

        for page in page_iterator:
            if 'Contents' in page:
                for obj in page['Contents']:
                    if obj['Key'].endswith(".pdf"):
                        try:
                            response = s3.get_object(Bucket=bucket_name, Key=obj['Key'])
                            file_content = response['Body'].read()
                            xml_data = parser.from_buffer(file_content, xmlContent=True)
                            all_pdf_data.append((obj['Key'], xml_data['content'], xml_data['metadata']))
                        except Exception as e:
                            print(f"Error processing {obj['Key']}: {e}")

        return all_pdf_data
    except Exception as e:
        print(f"An error occurred: {e}")
        return []

def pre_process_text(text_data):
    """
    Cleans text by removing HTML tags and entities.
    """
    return re.sub(r"<[^>]*>|&\w+;", "", text_data)

def extract_pdf_metadata_page(file_content, metadata, file_name):
    """
    Extracts metadata and content of a PDF file.
    """
    soup = BeautifulSoup(file_content, "html.parser")
    pages = soup.find_all("div", class_="page")
    total_pages = len(pages)

    metadata_fields = {
        "total_pages": metadata.get('meta:page-count', total_pages),
        "title": metadata.get('pdf:docinfo:title', ""),
        "keywords": metadata.get('pdf:docinfo:keywords', ""),
        "create_date": metadata.get('xmp:CreateDate', ""),
        "modify_date": metadata.get('xmp:ModifyDate', "")
    }

    doc_list = []
    for page_num, page in enumerate(pages, start=1):
        page_content = pre_process_text(str(page))
        doc = {
            "file_name": file_name,
            "total_pages": metadata_fields["total_pages"],
            "keywords": metadata_fields["keywords"],
            "create_date": metadata_fields["create_date"],
            "modify_date": metadata_fields["modify_date"],
            "page_content": page_content,
            "page_number": page_num,
            "content_length": len(page_content)
        }
        doc_list.append(doc)

    return doc_list

def preprocess_documents(documents):
    """
    Prepares documents by separating content and metadata.
    """
    content = []
    metadata = []

    for doc in documents:
        metadata.append({
            "title": doc.get("file_name", ""),
            "document_url": doc.get("file_name", ""),
            "page_number": doc.get("page_number", "")
        })
        content.append(f"Document Content: {doc.get('page_content', '')}")

    return content, metadata

def get_embedding(input_text, model=text_embedding_model):
    """
    Fetches embedding for a given text using the specified model.
    """
    try:
        response = embeddings.create(model_name=model, input=input_text)
        return response.data[0].embedding
    except Exception as e:
        print(f"Error generating embedding: {e}")
        return None

def run_vector_search(query, cursor, table_name, metric="COSINE_SIMILARITY", k=4):
    """
    Performs vector search on indexed documents.
    """
    try:
        query_vector = get_embedding(query)
        if not query_vector:
            raise ValueError("Failed to generate query embedding.")

        sort_order = "DESC" if metric != "L2DISTANCE" else "ASC"
        sql_query = f"""
        SELECT TOP {k} ID, PAGE_NUMBER, TEXT
        FROM {table_name}
        ORDER BY {metric}(VECTOR_STR, TO_REAL_VECTOR('{query_vector}')) {sort_order}
        """
        cursor.execute(sql_query)
        return cursor.fetchall()
    except Exception as e:
        print(f"Error during vector search: {e}")
        return []

if __name__ == "__main__":
    # Load PDF data
    all_pdf_data = parse_pdfs_from_s3_buffer(parameters["bucket"], parameters["path_prefix"])

    # Process PDFs
    doc_list = []
    if all_pdf_data:
        for file_key, content, metadata in all_pdf_data:
            docs = extract_pdf_metadata_page(content, metadata, file_key)
            doc_list.extend(docs)

    # Preprocess documents
    content, metadata = preprocess_documents(doc_list)

    # Split documents
    text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
        chunk_size=parameters["ingestion_chunk_size"],
        chunk_overlap=parameters["ingestion_chunk_overlap"]
    )
    split_docs = text_splitter.create_documents(content, metadatas=metadata)

    # Generate unique IDs and filter duplicates
    id_list = [hashlib.sha256(doc.page_content.encode()).hexdigest() for doc in split_docs]
    unique_docs = len(set(id_list))
    print(f"{len(id_list) - unique_docs} duplicate documents found.")
    print(f"{unique_docs} out of {len(split_docs)} unique documents will be indexed.")

    # Connect to HANA DB
    try:
        connection = dbapi.connect(
            address=HANA_DB_CONFIG["HOST"],
            port=443,
            user=HANA_DB_CONFIG["USER"],
            password=HANA_DB_CONFIG["PASSWORD"],
            encrypt=True
        )
        cursor = connection.cursor()

        # Hana db table name
        table_name = HANA_DB_CONFIG["TABLE_NAME"]
        print(f"Checking if table {table_name} is exist")
        query = """
        SELECT COUNT(*)
        FROM M_TABLES
        WHERE TABLE_NAME = ?
        """
        cursor.execute(query, (table_name.upper()))
        result = cursor.fetchone()
        is_exist = result[0]
        if is_exist > 0:
            print(f"table is already existed, deleting the table {table_name}")
            cursor.execute(f"DROP TABLE {table_name}")

        print(f"creating table {table_name}")
        cursor.execute(f"""
            CREATE COLUMN TABLE {table_name} (
                ID BIGINT, 
                TITLE NVARCHAR(100), 
                PAGE_NUMBER NVARCHAR(5), 
                URL NVARCHAR(100), 
                TEXT NCLOB, 
                VECTOR_STR REAL_VECTOR
            )
        """)

        # Insert documents
        sql_insert = f"INSERT INTO {table_name} (ID, TITLE, PAGE_NUMBER, URL, TEXT, VECTOR_STR) VALUES (?,?,?,?,?,TO_REAL_VECTOR(?))"
        # Prepare documents for indexing
        print("Generating embedding....")
        document_to_index = []
        for idx, doc in enumerate(split_docs):
            try:
                embed = get_embedding(doc.page_content)
                time.sleep(0.500)

                if embed is None:
                    continue  # Skip documents with failed embeddings
                document_to_index.append([
                    idx + 1,
                    doc.metadata.get('title', ''),
                    doc.metadata.get('page_number', ''),
                    doc.metadata.get('document_url', ''),
                    doc.page_content,
                    str(embed)
                ])
            except Exception as e:
                print(f"Error processing document {idx + 1}: {e}")
        
        cursor.executemany(sql_insert, document_to_index)
        print(f"{len(document_to_index)} documents inserted into the table.")

        # Test vector search
        question = "Overview of the Granite Pre-Training Dataset?"
        results = run_vector_search(question, cursor, table_name)
        print("#" * 100)
        print(f"Search results: {results}")
        print("#" * 100)
    except Exception as e:
        print(f"Error: {e}")

    finally:
        if 'connection' in locals():
            connection.close()