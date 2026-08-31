# Lab — Bagging, Boosting et Ensemble Learning

| Modèle                  | Accuracy (Test) | Temps d'entraînement (s) | Observations / Surapprentissage                                                                                                     |
| :----------------------- | :-------------: | :-----------------------: | :---------------------------------------------------------------------------------------------------------------------------------- |
| **Arbre Seul**     |      0.840      |          0.0290          | Accuracy Train = 1.000. Écart important avec le test → fort surapprentissage.                                                     |
| **Bagging manuel** |      0.912      |          0.9641          | Amélioration importante par rapport à l'arbre seul. Réduction du surapprentissage grâce à la combinaison de plusieurs arbres.  |
| **Random Forest**  |      0.946      |          0.2174          | Meilleur résultat avec`max_features=5`. Accuracy Train = 1.000, Test = 0.946 → léger écart.                                   |
| **AdaBoost**       |      0.812      |          0.2059          | Meilleur résultat avec`learning_rate=1.0`. Accuracy Train = 0.8345, Test = 0.812.                                                |
| **XGBoost**        |      0.928      |          0.2748          | Accuracy Train = 0.9795, Test = 0.928 → écart modéré et certain surapprentissage.                                               |
| **Stacking**       |      0.960      |          2.5510          | Meilleure Accuracy Test. Combinaison de plusieurs modèles avec une Régression Logistique comme méta-modèle. Temps plus élevé. |

## Analyse des résultats

### Arbre de décision seul

* **Accuracy Train** : 1.000
* **Accuracy Test** : 0.840
* **Temps** : 0.029 s

L'Accuracy de 100 % sur les données d'entraînement montre que l'arbre a parfaitement appris les données de train. Cependant, son Accuracy chute à 84 % sur les données de test.

Cela indique un **surapprentissage important** : l'arbre mémorise trop précisément les données d'entraînement et généralise moins bien sur de nouvelles données.

### Bagging manuel

* **Accuracy Test** : 0.912
* **Temps** : 0.964 s

---

### Random Forest

Plusieurs valeurs de `max_features` ont été testées :

| max_features | Accuracy Train |  Accuracy Test  |    Temps (s)    |
| :----------: | :-------------: | :-------------: | :--------------: |
|      2      |      1.000      |      0.928      |      0.1366      |
| **5** | **1.000** | **0.946** | **0.2174** |
|      10      |      1.000      |      0.942      |      0.3806      |
|      15      |      1.000      |      0.938      |      0.5491      |
|      20      |     0.9995     |      0.914      |      0.7220      |

La meilleure configuration testée est :

* **max_features** = 5
* **Accuracy Test** = 0.946

La limitation des variables à chaque nœud (`max_features`) améliore la diversité des arbres par rapport à un Bagging classique. Chaque arbre possède ses propres features, contrairement au Bagging classique où les arbres partagent les mêmes features mais se différencient uniquement par leurs échantillons de données.

---

### AdaBoost

Plusieurs valeurs de `learning_rate` ont été testées :

| learning_rate |  Accuracy Train  |  Accuracy Test  |
| :-----------: | :--------------: | :-------------: |
|     0.01     |      0.690      |      0.682      |
|      0.1      |      0.7955      |      0.784      |
|      0.5      |      0.8285      |      0.798      |
| **1.0** | **0.8345** | **0.812** |
|      2.0      |      0.3275      |      0.316      |

La meilleure valeur testée est `learning_rate=1.0`, avec une Accuracy Test de 0.812.

Plus le `learning_rate` augmente, plus l'accuracy s'améliore. Néanmoins, lorsqu'il devient trop grand (ici égal à 2.0), cela diminue fortement l'accuracy. Une valeur trop faible peut conduire à un apprentissage insuffisant, tandis qu'une valeur trop élevée dégrade fortement les performances dans cette expérience.

---

### XGBoost

* **Accuracy Train** : 0.9795
* **Accuracy Test** : 0.928
* **Temps** : 0.2748 s

L'écart entre le train et le test est de : `0.9795 - 0.928 = 0.0515`

---

### Stacking

* **Accuracy Test** : 0.960
* **Temps** : 2.551 s

Le Stacking combine plusieurs modèles de base :

* Régression Logistique
* SVM
* Random Forest
* XGBoost

Une **Régression Logistique** est utilisée comme méta-modèle afin d'apprendre comment combiner au mieux les prédictions de ces modèles de base. Avec une Accuracy de 0.960, le Stacking obtient la **meilleure performance** parmi l'ensemble des modèles évalués. En contrepartie, son temps d'entraînement est plus important.

#### Focus sur les stratégies de Voting

Deux stratégies de Voting ont également été testées en parallèle :

| Méthode              |  Accuracy Test  |    Temps (s)    |
| :-------------------- | :-------------: | :-------------: |
| Hard Voting           |      0.918      |      0.509      |
| **Soft Voting** | **0.958** | **0.502** |

Le **Soft Voting** s'avère plus performant que le Hard Voting  :

* Le **Hard Voting** choisit la classe majoritaire ayant obtenu le plus de votes bruts.
* Le **Soft Voting** s'appuie sur les probabilités prédites par chaque modèle et calcule une moyenne pondérée de ces probabilités pour prendre sa décision.
