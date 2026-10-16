from rag import ask

 

TESTS = [

    ("Combien de jours de congés payés par an ?", "25"),

    ("Combien de jours de télétravail par semaine ?", "3"),

    ("Quel est le jour de présence obligatoire ?", "mardi"),

    ("Quelle est l'indemnité de télétravail ?", "20"),

    ("Tous les combien de jours change-t-on son mot de passe ?", "90"),

    ("Quelle est la capitale de l'Australie ?", "ne sais pas"),

]

ok = 0

for q, attendu in TESTS:

    rep, src = ask(q)

    good = attendu.lower() in rep.lower()

    ok += good

    print("OK   " if good else "ECHEC", q, "->", rep[:80])

print(f"Score : {ok}/{len(TESTS)}")