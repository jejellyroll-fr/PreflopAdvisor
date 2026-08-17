# Plan d'implémentation — onglet Configuration

> Analyse de `feature/pyside` au commit `b24da69`. Tous les chiffres cités ont été mesurés
> sur cette révision, pas estimés.

## 1. Verdict

Éditer un fichier INI à la main est aujourd'hui le **seul** moyen de faire les gestes
courants : ajouter une sim, déclarer un ante, dire ce que vaut un sizing, changer le nombre
de sièges. Ce n'est pas une gêne théorique — trois occurrences dans la seule session qui a
produit ce document :

- sept sims fictives générées n'apparaissaient pas dans le sélecteur, parce que leurs lignes
  étaient commentées dans `config.ini` ;
- les chemins de `[TreeToolTips]` livrés pointent vers `/home/johann/code/…`, la machine de
  l'auteur d'origine, et n'ont jamais pu fonctionner ailleurs ;
- `TableN.ante` et `<sizing>.pot` / `.blinds` ne sont découvrables que par la prose du
  README ; rien dans l'application ne dit qu'ils existent, ni qu'ils manquent.

Le README lui-même finit par écrire, à propos des codes de sizing : *« you have to figure it
out by yourself / check the filenames in ranges folder »*. C'est la mesure exacte du trou :
l'information est dans les fichiers de la sim, l'application sait déjà la lire, et
l'utilisateur doit la retrouver à la main.

Un onglet Configuration n'est donc pas un confort. C'est l'endroit où trois choses que
l'application connaît déjà — quels codes contient une sim, lesquels elle sait décoder,
quelles sims répondent — cessent d'être invisibles.

---

## 2. Cartographie de l'existant

### 2.1 Ce que la configuration contient réellement

Inventaire obtenu en cherchant les accès effectifs (`.get("clé")`, `_setting("clé")`,
`section["clé"]`) dans le paquet, pas les occurrences textuelles.

| Section | Clés **vivantes** | Clés **mortes** |
|---|---|---|
| `[CardSelector]` | `NumCards`, `ButtonPad` | `ButtonHeight`, `ButtonWidth`, `Background`, `BackgroundPressed` |
| `[TreeSelector]` | `FontSize`, `Font`, `ToolTips`, `DefaultTree` | `ButtonHeight`, `ButtonWidth`, `Background`, `BackgroundPressed` |
| `[TreeSelector]` | `NumTrees` — **lu mais seulement journalisé**, aucun effet | |
| `[TreeInfos]` | les sims, `TableN.ante` | |
| `[TreeToolTips]` | une image ou un texte par sim | |
| `[TreeReader]` | noms d'actions → codes, `<nom>.pot`, `<nom>.blinds`, `Ending`, `Positions`, `Positions7/8/9`, `RaiseSizeList`, `ValidActions`, `CacheSize`, `UseDatabase` | |
| `[PositionSelector]` | `PositionList`, `PositionInactive`, `ButtonHeight`, `ButtonWidth`, `ButtonPad`, `FontSize`, `Font`, `DefaultPosition` | `Background`, `BackgroundPressed` — **lues dans des attributs jamais appliqués** (`position_selector.py:42-43`) |
| `[Output]` | `AdjustFoldEV`, `ChipsPerBB`, `FontSize`, `Font` | `Height`, `Width`, `Background`, `InfoFontSize`, `InfoFont` |

Les clés mortes sont l'héritage Tkinter : les couleurs et les tailles appartiennent
maintenant à `theme.py` et au QSS. Elles ne doivent pas apparaître dans l'onglet — elles
doivent **disparaître du fichier**, sinon l'onglet documentera des réglages sans effet.

### 2.2 Ce qui n'est atteignable par aucun réglage

Les codes d'action (`Fold=0`, `Raise100=40100`, `3xOpen=15`…) ne sont pas lus par leur nom :
ils sont lus par indirection, à travers `ValidActions` et `RaiseSizeList`. Ajouter un sizing,
c'est donc **deux** éditions cohérentes entre elles — une clé `Nom=code`, et `Nom` ajouté à
la liste. Une seule des deux, et la cellule reste vide sans rien dire. C'est précisément le
genre d'erreur qu'un formulaire supprime par construction.

### 2.3 Où la configuration est écrite aujourd'hui

Nulle part. `config.ini` est lu, jamais écrit. Seules la géométrie de la fenêtre et la
position du séparateur sont persistées, via `QSettings`, dont l'identité
(`SETTINGS_ORGANIZATION` / `SETTINGS_APPLICATION`) est déjà déclarée par les deux points
d'entrée.

---

## 3. La décision de conception : où écrire

`gui.py` lit `package_file("config.ini")`, c'est-à-dire **le fichier à l'intérieur du
paquet**. Y écrire depuis l'application est faux dans les trois situations réelles :

| Situation | Conséquence d'une écriture dans le paquet |
|---|---|
| Installation pip / binaire PyInstaller | `site-packages` ou l'archive gelée : perdu à la mise à jour, parfois en lecture seule |
| Clone de développement | l'application salit l'arbre git de l'utilisateur à chaque réglage |
| Poste partagé | un utilisateur écrit la configuration de tous |

**Et surtout** : réécrire ce fichier avec `ConfigParser` détruit ses commentaires. Mesuré —
**107 lignes de commentaires avant, 0 après**, 189 lignes réduites à 76. Disparaîtraient au
premier clic sur « Enregistrer » : les sims d'exemple commentées de l'auteur d'origine, les
explications sur les sièges, la documentation des sizings. C'est aussi pourquoi le
commutateur `scripts/make_test_trees.py --enable` édite le fichier **ligne à ligne en
texte**, et pas via `ConfigParser`.

### Proposition : deux fichiers, en couches

```
préréglages du paquet   preflop_advisor/config.ini   lu seul, jamais écrit, garde ses commentaires
        ↓ recouvert par
configuration utilisateur  <AppConfigLocation>/config.ini   écrit par l'application, sans commentaires
```

- lecture : `ConfigParser` lit les deux dans l'ordre, le second gagne clé par clé ;
- écriture : uniquement le second, et **uniquement les clés qui diffèrent** du préréglage,
  pour qu'une amélioration des valeurs livrées profite à qui n'y a pas touché ;
- emplacement : `QStandardPaths.AppConfigLocation`, cohérent avec le `QSettings` déjà
  utilisé pour la géométrie ;
- un bouton par section pour revenir au préréglage : supprimer la clé du fichier utilisateur
  suffit, ce qui est une opération sûre et lisible.

Cette couche est le vrai travail. L'interface, ensuite, est du formulaire.

---

## 4. Périmètre

### 4.1 Dans l'onglet

| Panneau | Ce qu'il édite | Pourquoi il est là |
|---|---|---|
| **Sims** | `[TreeInfos]` : nom, joueurs, tapis, jeu, dossier, description, `TableN.ante`, tooltip | le geste le plus fréquent, et le seul qui casse aujourd'hui en silence |
| **Sizings** | `[TreeReader]` : table nom → code, ordre de `RaiseSizeList`, `ValidActions`, `<nom>.pot` / `.blinds` | deux éditions à garder cohérentes, aujourd'hui à la main |
| **Sièges** | `Positions`, `Positions7/8/9`, `PositionList`, `PositionInactive`, `DefaultPosition` | dépend de la taille de table, invisible depuis l'interface |
| **Lecture** | `CacheSize`, `UseDatabase`, `Ending` | arbitrage mémoire / vitesse, aujourd'hui aveugle |
| **Affichage** | `ChipsPerBB`, `AdjustFoldEV`, `ToolTips`, `FontSize` | `ChipsPerBB` change tous les EV affichés |

### 4.2 Hors de l'onglet, volontairement

- **les clés mortes** — à supprimer du fichier livré dans la même livraison, pas à afficher ;
- **`NumTrees`** — sans effet ; à supprimer aussi ;
- **le thème et les couleurs** — `theme.py` en est propriétaire ; un onglet qui offrirait
  `Background=white` mentirait ;
- **la géométrie de la fenêtre** — déjà persistée, et pas un réglage : un souvenir.

---

## 5. Ce que l'onglet doit savoir faire, et que l'application sait déjà

C'est la partie qui justifie l'onglet plutôt qu'un éditeur de texte.

### 5.1 Découvrir les sizings d'une sim

`ActionProcessor` construit déjà un index `_nodes` des lignes présentes dans un dossier, et
`sizings.sizing_for_code` sait dire d'un code s'il a un sens publié. En les croisant :

> Cette sim utilise les codes `0`, `1`, `40075`, `40100`, `15`.
> `15` n'a pas de signification publiée — le trainer dessinera sans pot les mains qui
> passent par lui. Déclarez ce qu'il vaut : ○ % du pot ○ big blinds.

C'est exactement le « figure it out by yourself » du README, rendu à l'application.

### 5.2 Vérifier une sim avant de l'accepter

Trois contrôles, dont deux existent déjà quelque part :

- le dossier existe-t-il — `paths.resolve_range_folder` le fait, et retrouve même un chemin
  absolu périmé venu d'une autre machine. Mais il ne vérifie **pas** que le dossier contient
  des fichiers de range : ce contrôle-là n'existe que dans `scripts/frequency_report.py`
  (`holds_range_files`), pas dans le paquet. À remonter dans `paths.py` en phase 3 ;
- le nombre de joueurs déclaré correspond-il aux sièges que les noms de fichiers impliquent ;
- la description mentionne-t-elle un ante sans que `TableN.ante` soit déclaré — cas où
  l'application masque déjà pot et tapis, sans jamais dire pourquoi.

### 5.3 Dire ce qui prend effet quand

| Réglage | Effet |
|---|---|
| `ChipsPerBB`, `AdjustFoldEV`, `ToolTips`, ante, sizings | immédiat — la grille se redessine depuis `_last_render` |
| liste des sims, tooltips | immédiat — le sélecteur se reconstruit |
| `Positions*`, `PositionList` | immédiat pour la sim suivante ; le `TreeReader` est reconstruit à chaque lecture |
| `CacheSize`, `UseDatabase`, `Ending` | au prochain chargement de sim — le cache et le store sont tenus par processus |

Aucun réglage ne doit exiger un redémarrage sans le dire.

---

## 6. Découpage

| Phase | Contenu | Livrable vérifiable |
|---|---|---|
| **0** | Ménage : supprimer les clés mortes du fichier livré et le code qui les lit sans les appliquer | `config.ini` ne contient plus que des réglages ayant un effet ; tests inchangés |
| **1** | Couche de configuration en deux fichiers : lecture en couches, écriture des seules différences, retour au préréglage | tests unitaires sans Qt : couches, écriture minimale, fichier utilisateur absent, illisible, partiel |
| **2** | Onglet + panneaux **Affichage** et **Lecture** — les réglages scalaires, les plus simples | l'onglet existe, `ChipsPerBB` modifié redessine la grille |
| **3** | Panneau **Sims** : tableau éditable, ajout, suppression, sélecteur de dossier, ante, tooltip, les trois contrôles de 5.2 | ajouter une sim sans toucher au fichier ; une sim invalide est refusée avec sa raison |
| **4** | Panneau **Sizings** : table nom/code, ordre de `RaiseSizeList`, déclarations `.pot` / `.blinds`, et la découverte de 5.1 | une sim aux codes inconnus se déclare depuis l'interface, le pot réapparaît |
| **5** | Panneau **Sièges** | une sim 7-max nomme ses sièges depuis l'interface |
| **6** | Documentation : le README cesse de décrire des éditions manuelles | les sections « Configure the Tool » et `RaiseSizeList` renvoient à l'onglet |

Les phases 0 et 1 n'ont pas d'interface et sont le vrai risque ; elles se testent sans Qt.
Les phases 2 à 5 sont indépendantes entre elles et livrables une par une.

---

## 7. Tests

Le seuil du projet est à 90 % et la suite compte 504 tests ; cette fonctionnalité ne doit pas
être l'exception.

- **Couche de configuration** (sans Qt) : le fichier utilisateur recouvre le préréglage clé
  par clé ; seules les différences sont écrites ; une valeur remise au préréglage disparaît
  du fichier ; un fichier utilisateur illisible n'empêche pas le démarrage.
- **Panneaux** (`pytest-qt`) : modifier une valeur l'écrit ; le formulaire refuse une sim
  sans fichiers de range ; un sizing déclaré rend son pot au trainer.
- **Non-régression** : le fichier livré, après suppression des clés mortes, produit la même
  application — la grille et le trainer sont identiques à ceux d'avant la phase 0.
- **Le piège des commentaires** : un test qui écrit la configuration utilisateur puis vérifie
  que `preflop_advisor/config.ini` n'a pas changé d'un octet. C'est la garantie que le fichier
  livré, ses exemples et sa documentation survivent à l'onglet.

---

## 8. Risques et questions ouvertes

- **Propriété du fichier après mise à jour.** N'écrire que les différences limite la casse,
  mais une clé renommée en amont laisse un réglage orphelin. À traiter par une liste de clés
  connues, et un avertissement journalisé sur les autres — jamais une suppression silencieuse.
- **Qui gagne, le fichier ou l'onglet, si l'utilisateur édite les deux ?** Proposition :
  l'onglet relit le fichier à l'ouverture, donc une édition manuelle est visible ; mais deux
  écritures concurrentes ne sont pas gérées. Acceptable pour une application mono-poste.
- **Migration des `[TreeToolTips]` livrés.** Ils pointent vers une machine qui n'existe pas.
  Les vider dans la phase 0 est un changement visible ; à valider plutôt qu'à décider seul.
- **`Ending`.** Modifiable en théorie, mais un dossier de `.rng` lu avec une autre extension
  ne répond plus rien. Peut-être à afficher sans permettre l'édition.
- **Le nombre de sims que l'interface tient.** Le sélecteur est une liste déroulante ; à dix
  sims fictives plus les vraies, elle reste lisible. Non mesuré au-delà.
