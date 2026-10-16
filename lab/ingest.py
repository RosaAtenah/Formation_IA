

from langchain_community.document_loaders import DirectoryLoader, TextLoader

from langchain_text_splitters import RecursiveCharacterTextSplitter

from langchain_ollama import OllamaEmbeddings

from langchain_chroma import Chroma

 

loader = DirectoryLoader("data", glob="**/*.txt", loader_cls=TextLoader,

                         loader_kwargs={"encoding": "utf-8"})

docs = loader.load()

print(len(docs), "documents chargés")

 

splitter = RecursiveCharacterTextSplitter(chunk_size=400, chunk_overlap=60)

chunks = splitter.split_documents(docs)

print(len(chunks), "chunks créés")

print("Exemple :", chunks[0].page_content[:120], "|", chunks[0].metadata)

 

emb = OllamaEmbeddings(model="nomic-embed-text")

Chroma.from_documents(chunks, emb, persist_directory="db")

print("Index enregistré dans db/")