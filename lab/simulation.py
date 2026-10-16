from rag import ask

 

SCENARIO = [

    "Combien de jours de congés payés ai-je par an ?",

    "Quel jour dois-je venir au bureau ?",

    "Comment joindre le support ?",

    "Qui a gagné la Coupe du monde 2018 ?",

]

for q in SCENARIO:

    answer, sources = ask(q)

    print("Vous >", q)

    print("Bot  >", answer)

    print("       [", ", ".join(sources), "]\n")