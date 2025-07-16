import os
import time
import json
import logging
import re
from datetime import datetime, timedelta

import azure.functions as func

from azure.identity import DefaultAzureCredential
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.cognitiveservices.models import CognitiveServicesAccountCreateParameters, Sku
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
from azure.search.documents.indexes import SearchIndexClient
from azure.search.documents.indexes.models import (
    SearchIndex,
    SimpleField,
    SearchableField,
    SearchFieldDataType,
    VectorSearch,
    VectorSearchAlgorithmConfiguration,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Global variable for storage key (set after storage account creation)
AZURE_STORAGE_ACCOUNT_KEY = None

def print_info(msg: str):
    logger.info(msg)

def print_success(msg: str):
    logger.info(f"SUCCESS: {msg}")

def print_error(msg: str):
    logger.error(f"ERROR: {msg}")


async def main(req: func.HttpRequest) -> func.HttpResponse:
    logger.info('Processing create_db request...')

    try:
        req_body = req.get_json()
    except ValueError:
        return func.HttpResponse("Invalid JSON body", status_code=400)

    title = req_body.get("title")
    description = req_body.get("description", "")
    tags = req_body.get("tags", [])

    if not title or not isinstance(title, str) or not title.strip():
        return func.HttpResponse("Missing or invalid 'title' in request body", status_code=400)

    # Sanitize title to be lowercase alphanumeric for resource naming (max 24 chars for storage/search account)
    sanitized_title = re.sub(r"[^a-z0-9]", "", title.lower())[:20]

    subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")
    resource_group = os.environ.get("AZURE_RESOURCE_GROUP")
    location = os.environ.get("AZURE_LOCATION", "eastus")
    storage_account_base = os.environ.get("STORAGE_ACCOUNT_BASE", "mystorageacct")
    search_account_base = os.environ.get("SEARCH_ACCOUNT_BASE", "mysearchacct")
    index_name = os.environ.get("SEARCH_INDEX_NAME", "rag-vector-index")

    if not subscription_id or not resource_group:
        return func.HttpResponse(
            "Missing required environment variables: AZURE_SUBSCRIPTION_ID or AZURE_RESOURCE_GROUP",
            status_code=500
        )

    try:
        credential = DefaultAzureCredential()
        storage_client = StorageManagementClient(credential, subscription_id)
        cog_client = CognitiveServicesManagementClient(credential, subscription_id)

        # Create Storage Account
        storage_account_name = f"{storage_account_base}{int(time.time()) % 10000}"
        storage_config = await create_storage_account(storage_client, resource_group, storage_account_name, location)

        global AZURE_STORAGE_ACCOUNT_KEY
        AZURE_STORAGE_ACCOUNT_KEY = storage_config["AZURE_STORAGE_KEY"]
        AZURE_STORAGE_URL = storage_config["AZURE_STORAGE_URL"]

        # Create Vector Search Service and Index
        search_account_name = f"{search_account_base}{int(time.time()) % 10000}"
        vector_db_config = await create_vector_database_and_index(
            cog_client,
            resource_group,
            search_account_name,
            location,
            index_name
        )

        # Return JSON response with all configs
        response_payload = {
            "storage": storage_config,
            "vector_search": vector_db_config
        }

        return func.HttpResponse(
            json.dumps(response_payload),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        logger.error(f"Error in main function: {e}", exc_info=True)
        return func.HttpResponse(f"Error: {e}", status_code=500)
    

async def create_storage_account(storage_client: StorageManagementClient, resource_group_name: str, storage_name: str, location: str):
    print_info(f"Creating storage account '{storage_name}' in '{location}'...")
    try:
        try:
            storage_client.storage_accounts.get_properties(resource_group_name, storage_name)
            print_info(f"Storage account '{storage_name}' already exists.")
        except:
            poller = storage_client.storage_accounts.begin_create(
                resource_group_name,
                storage_name,
                {
                    "sku": {"name": "Standard_LRS"},
                    "kind": "StorageV2",
                    "location": location,
                    "enable_https_traffic_only": True
                }
            )
            poller.result()
            print_success(f"Storage account '{storage_name}' created.")

        keys = storage_client.storage_accounts.list_keys(resource_group_name, storage_name)
        storage_key = keys.keys[0].value
        storage_url = f"https://{storage_name}.blob.core.windows.net"

        return {
            "AZURE_STORAGE_URL": storage_url,
            "AZURE_STORAGE_NAME": storage_name,
            "AZURE_STORAGE_KEY": storage_key
        }
    except Exception as e:
        print_error(f"Failed to create storage account: {e}")
        raise

def ensure_container_exists(blob_service_client: BlobServiceClient, container_name: str):
    print_info(f"Checking container '{container_name}'.")
    container_client = blob_service_client.get_container_client(container_name)
    try:
        container_client.create_container()
        print_success(f"Created container '{container_name}'.")
    except Exception as e:
        print_info(f"Container '{container_name}' likely exists or could not be created: {e}")
    return container_client

async def upload_blob_and_generate_sas(blob_service_client: BlobServiceClient, container_name: str, blob_name: str, data: bytes, sas_expiry_hours=1):
    print_info(f"Uploading blob '{blob_name}' to container '{container_name}'.")
    container_client = ensure_container_exists(blob_service_client, container_name)
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(data, overwrite=True)
    print_success(f"Uploaded blob '{blob_name}' to container '{container_name}'.")
    print_info(f"SAS URL generating for blob '{blob_name}'.")
    sas_token = generate_blob_sas(
        blob_service_client.account_name,
        container_name,
        blob_name,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=sas_expiry_hours),
        account_key=AZURE_STORAGE_ACCOUNT_KEY
    )
    blob_url = f"https://{blob_service_client.account_name}.blob.core.windows.net/{container_name}/{blob_name}"
    blob_url_with_sas = f"{blob_url}?{sas_token}"
    print_success(f"SAS URL generated for blob '{blob_name}'.")
    return blob_url_with_sas

async def create_vector_database_and_index(
    cog_client: CognitiveServicesManagementClient,
    resource_group_name: str,
    search_account_name: str,
    location: str,
    index_name: str,
    admin_key: str = None
):
    print_info(f"Creating Cognitive Search service '{search_account_name}' in '{location}'...")
    try:
        try:
            cog_client.accounts.get(resource_group_name, search_account_name)
            print_info(f"Cognitive Search service '{search_account_name}' already exists.")
        except Exception:
            params = CognitiveServicesAccountCreateParameters(
                location=location,
                sku=Sku(name="S1"),
                kind="Search"
            )
            poller = cog_client.accounts.begin_create(
                resource_group_name,
                search_account_name,
                params
            )
            poller.result()
            print_success(f"Cognitive Search service '{search_account_name}' created.")
        
        if not admin_key:
            keys = cog_client.accounts.list_keys(resource_group_name, search_account_name)
            admin_key = keys.primary_key
            print_info(f"Retrieved admin key for Search service '{search_account_name}'.")

        endpoint = f"https://{search_account_name}.search.windows.net"
        search_client = SearchIndexClient(endpoint=endpoint, credential=admin_key)

        vector_search = VectorSearch(
            algorithm_configurations=[
                VectorSearchAlgorithmConfiguration(
                    name="my-vector-config",
                    kind="hnsw",
                    parameters={
                        "m": 4,
                        "efConstruction": 400,
                        "efSearch": 200
                    }
                )
            ]
        )

        fields = [
            SimpleField(name="id", type=SearchFieldDataType.String, key=True),
            SearchableField(name="content", type=SearchFieldDataType.String, searchable=True, analyzer_name="en.lucene"),
            SimpleField(name="metadata_author", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(name="metadata_category", type=SearchFieldDataType.String, filterable=True, facetable=True),
            SimpleField(
                name="contentVector",
                type=SearchFieldDataType.Collection(SearchFieldDataType.Single),
                searchable=True,
                vector_search_dimensions=1536,
                vector_search_configuration="my-vector-config"
            )
        ]

        index = SearchIndex(
            name=index_name,
            fields=fields,
            vector_search=vector_search
        )

        print_info(f"Creating/updating search index '{index_name}'...")
        search_client.create_or_update_index(index)
        print_success(f"Search index '{index_name}' created/updated.")

        return {
            "search_endpoint": endpoint,
            "search_admin_key": admin_key,
            "index_name": index_name
        }

    except Exception as e:
        print_error(f"Failed to create vector database and index: {e}")
        raise
