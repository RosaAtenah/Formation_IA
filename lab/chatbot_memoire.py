from langchain_core.prompts import ChatPromptTemplate

from langchain_core.output_parsers import StrOutputParser

from rag import ask, llm

 

rewrite = (ChatPromptTemplate.from_template(

    "Historique :\n{history}\n\n"

    "Reformule la dernière question en question autonome, sans y répondre.\n"

    "Dernière question : {question}\nQuestion autonome :")

    | llm | StrOutputParser())

 

history = []

while True:

    q = input("\nVous > ").strip()

    if q.lower() in ("quit", "exit", "q"):

        break

    if not q:

        continue

    standalone = q

    if history:

        text = "\n".join(f"Utilisateur: {u}\nBot: {b}" for u, b in history[-3:])

        standalone = rewrite.invoke({"history": text, "question": q}).strip()

        print("  [question reformulée]", standalone)

    answer, sources = ask(standalone)

    history.append((q, answer))

    print("Bot >", answer)

    print("Sources :", ", ".join(sources))