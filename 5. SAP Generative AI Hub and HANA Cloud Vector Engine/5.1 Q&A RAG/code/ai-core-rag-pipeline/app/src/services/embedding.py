# Get embeddings
from dotenv import load_dotenv
load_dotenv()
from gen_ai_hub.proxy.native.openai import embeddings
import os

class Embedding:
    def __init__(self) -> None:
        pass

    def get_embedding_gen_ai(self, input) -> str:
        text_embedding_model = os.environ.get("EMBEDDING_MODEL", default="text-embedding-3-large")
        response = embeddings.create(
        model_name=text_embedding_model,
        input=input
        )
        return response.data[0].embedding