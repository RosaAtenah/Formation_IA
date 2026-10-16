from rag import ask

 

print("Chatbot RH Zenith Tech (tapez 'quit' pour sortir)")

while True:

    q = input("\nVous > ").strip()

    if q.lower() in ("quit", "exit", "q"):

        break

    if not q:

        continue

    answer, sources = ask(q)

    print("Bot >", answer)

    print("Sources :", ", ".join(sources))