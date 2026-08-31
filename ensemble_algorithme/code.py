from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier
from sklearn.metrics import accuracy_score
import numpy as np
from sklearn.ensemble import RandomForestClassifier , AdaBoostClassifier
from xgboost import XGBClassifier
import time
import matplotlib.pyplot as plt
from sklearn.linear_model import LogisticRegression
from sklearn.svm import SVC
from sklearn.ensemble import VotingClassifier , StackingClassifier


# generation des données
X, y = make_classification(
    n_samples=2500,
    n_features=20,
    n_informative=12,
    n_classes=2,
    random_state=42
)

# split des données
X_train, X_test, y_train, y_test = train_test_split(
    X, y,
    test_size=0.2,
    random_state=42
)

## PARTIE 1  / bagging_random_forest

# definition du modele
model = DecisionTreeClassifier(random_state=42)

start = time.time()

#entrainement du modele
model.fit(X_train , y_train)

end = time.time()

temps = end - start

y_train_pred = model.predict(X_train)
y_test_pred = model.predict(X_test)

print("Temps d'exécution Arbre Seul :", temps, "secondes")
print("Accuracy train :", accuracy_score(y_train, y_train_pred))
print("Accuracy test  :", accuracy_score(y_test, y_test_pred))


B = 50

models = []
start = time.time()

for b in range(B):
    indices = np.random.choice(
        len(X_train),
        size=len(X_train),
        replace=True
    )
    
    X_b = X_train[indices]
    y_b = y_train[indices]

    model = DecisionTreeClassifier(random_state=42)
    
    model.fit(X_b , y_b)

    models.append(model)

end = time.time()

temps = end - start
print("Temps d'exécution Bagging manuel :", temps, "secondes")

predictions = []

for tree in models :
    y_pred = tree.predict(X_test)
    predictions.append(y_pred)

predictions = np.array(predictions)

final_predictions = []
for i in range(len(X_test)):
    votes = predictions[:, i]
    vote_majoritaire = np.bincount(votes).argmax()
    final_predictions.append(vote_majoritaire)

accuracy_bagging = accuracy_score(
    y_test,
    final_predictions
)

print("Accuracy du Bagging :", accuracy_bagging)

max_features_values = [2, 5, 10, 15, 20]

for value in max_features_values:

    rf = RandomForestClassifier(
        n_estimators=50,
        max_features=value,
        random_state=42
    )
    start = time.time()

    rf.fit(X_train, y_train)
    end = time.time()

    temps = end - start

    train_score = rf.score(X_train, y_train)
    test_score = rf.score(X_test, y_test)

    print(
        "max_features =", value,
        "| Train =", train_score,
        "| Test =", test_score ,
        "Temps d'exécution  Random Forest :", temps
    )



## partie 2 / Boosting

stump = DecisionTreeClassifier(
    max_depth=1,
)

for lr in [0.01, 0.1, 0.5, 1.0, 2.0]:
    model = AdaBoostClassifier(
        n_estimators=50,
        estimator= stump,
        learning_rate=lr,
        random_state=42
    )
    start = time.time()

    model.fit(X_train, y_train)

    end = time.time()

    temps = end - start
    print(
        "learning_rate =", lr,
        "| Train =", model.score(X_train, y_train),
        "| Test =", model.score(X_test, y_test) ,
        "| Temps d'exécution  AdaBoost :", temps

    )




start = time.time()

xgb_model = XGBClassifier(
    n_estimators=200,
    learning_rate=0.1,
    max_depth=3,
    random_state=42,
    eval_metric="logloss"
)

xgb_model.fit(
    X_train,
    y_train,
    eval_set=[(X_train, y_train), (X_test, y_test)],
    verbose=False
)
end = time.time()

temps_xgb = end - start

print("Temps d'exécution XGBoost:", temps_xgb, "secondes")
print("Accuracy train :", xgb_model.score(X_train, y_train))
print("Accuracy test :", xgb_model.score(X_test, y_test))

results = xgb_model.evals_result()
train_logloss = results["validation_0"]["logloss"]
test_logloss = results["validation_1"]["logloss"]

plt.figure(figsize=(10, 6))

plt.plot(train_logloss, label="Train")
plt.plot(test_logloss, label="Test")

plt.xlabel("Nombre d'itérations")
plt.ylabel("Log-Loss")
plt.title("Courbe d'apprentissage XGBoost")

plt.legend()
plt.grid()
plt.show()


## Partie 3 :  Métamodèles — Voting & Stacking 



logistic = LogisticRegression(max_iter=1000)

svm = SVC(probability=True)

rf = RandomForestClassifier(
    n_estimators=50,
    random_state=42
)

xgb = XGBClassifier(
    n_estimators=50,
    learning_rate=0.1,
    max_depth=3,
    random_state=42,
    eval_metric="logloss"
)


hard_voting = VotingClassifier(
    estimators=[
        ("logistic", logistic),
        ("svm", svm),
        ("rf", rf),
        ("xgb", xgb)
    ],
    voting="hard"
)

start = time.time()

hard_voting.fit(X_train, y_train)
end = time.time()
temps = end - start
print(
    "Hard Voting :",
    hard_voting.score(X_test, y_test) , 
    " | Temps d'exécution  hard_voting :", temps

)

soft_voting = VotingClassifier(
    estimators=[
        ("logistic", logistic),
        ("svm", svm),
        ("rf", rf),
        ("xgb", xgb)
    ],
    voting="soft"
)

start = time.time()

soft_voting.fit(X_train, y_train)
end = time.time()
temps = end - start
print(
    "Soft Voting :",
    soft_voting.score(X_test, y_test) ,
    " | Temps d'exécution  soft_voting :", temps

)


stacking = StackingClassifier(
    estimators=[
        ("logistic", logistic),
        ("svm", svm),
        ("rf", rf),
        ("xgb", xgb)
    ],
    final_estimator=LogisticRegression(max_iter=1000)
)

start = time.time()

stacking.fit(X_train, y_train)
end = time.time()
temps = end - start

print(
    "Stacking :",
    stacking.score(X_test, y_test) , 
    " | Temps d'exécution  Stacking :", temps

)
