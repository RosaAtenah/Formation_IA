from langchain_ollama import OllamaEmbeddings, ChatOllama

from langchain_chroma import Chroma

from langchain_core.prompts import ChatPromptTemplate

from langchain_core.output_parsers import StrOutputParser

 

emb = OllamaEmbeddings(model="nomic-embed-text")

vs = Chroma(persist_directory="db", embedding_function=emb)

retriever = vs.as_retriever(search_kwargs={"k": 3})

llm = ChatOllama(model="llama3.2", temperature=0)

 

prompt = ChatPromptTemplate.from_template("""Tu es l'assistant RH de Zenith Tech.

Réponds uniquement à partir du contexte ci-dessous, en français, de façon concise.

Si la réponse n'est pas dans le contexte, réponds : "Je ne sais pas d'après les documents."

 

Contexte :

{context}

 

Question : {question}

Réponse :""")

 

chain = prompt | llm | StrOutputParser()

 

def fmt(docs):

    return "\n\n".join(d.page_content for d in docs)

 

def ask(question):

    docs = retriever.invoke(question)

    answer = chain.invoke({"context": fmt(docs), "question": question})

    sources = sorted({d.metadata.get("source", "?") for d in docs})

    return answer, sources