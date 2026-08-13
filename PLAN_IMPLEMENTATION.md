# Plan d'implémentation — PreflopAdvisor

> Analyse de la branche `feature/pyside` (commit `d680503`) — 3 370 lignes Python, PySide6, 7 tests, 53 % de couverture nominale.

---

## 1. Verdict

Le portage Tkinter → PySide6 est terminé sur le plan structurel, mais **le cœur métier est cassé** : aucune ligne de jeu contenant un *Raise* ne résout vers un fichier de range existant. Concrètement, l'application n'affiche aujourd'hui que les cellules `Fold`/`Call` de la ligne « first in » ; toutes les cellules *vs RFI*, *3bet*, *4bet*, *vs 4bet*, *squeeze*, *vs squeeze* sont vides, et la vue position `SB` lève une exception silencieuse.

La suite de tests est verte (7/7) parce qu'elle n'assert que des tautologies (`isinstance(results, list)`, `len(results) > 0`) : elle passe intégralement sur une application qui ne renvoie rien d'utile. **C'est le problème le plus grave** : il n'existe aucun filet de non-régression sur la couche solver.

Le plan ci-dessous est ordonné par valeur : d'abord réparer la lecture des ranges, ensuite verrouiller par des tests d'oracle, puis nettoyer l'architecture, enfin refondre l'UI.

---

## 2. Cartographie de l'existant

```
standalone.py ─┐
__main__.py ───┴──> gui.MainWindow
                     ├── card_selector.CardSelector      (grille 13×4, signal handChanged)
                     ├── tree_selector.TreeSelector      (QComboBox, signal treeChanged)
                     ├── position_selector.PositionSelector (signal positionChanged)
                     ├── randomizer.RandomButton         (isolé, aucun consommateur)
                     └── outputframe.OutputFrame
                          └── tree_reader.TreeReader          ← logique des lignes de jeu
                               └── tree_reader_helpers.ActionProcessor  ← I/O + cache + noms de fichiers
                                    └── hand_convert_helper.convert_hand ← normalisation main → format Monker

Hors graphe (mort ou non branché) :
  output_objects.py            0 % couverture, TableEntry dupliqué, jamais importé
  frequency_reader_helper.py   0 % couverture, chemin absolu en dur, jamais branché à la GUI
  tooltip.py                   utilisé uniquement via TreeSelector, chemins d'images tous commentés
```

**Format des ranges** (`ranges/HU-100bb-with-limp/*.rng`) : paires de lignes `main\nfréquence;EV`. Le nom de fichier encode la séquence d'actions via les codes Monker définis dans `[TreeReader]` (`Fold=0`, `Call=1`, `RaisePot=2`, `All_In=3`, `Raise100=40100`, `Raise75=40075`…), joints par des points. Exemple : `40100.1.rng` = SB raise pot-100, BB call.

---

## 3. Constats

### 3.1 — BLOQUANT : la résolution des tailles de relance produit des noms de fichiers inexistants

`tree_reader_helpers.py:157-181` (`find_valid_raise_sizes`) reconstruit une clé de config à partir de la **partie numérique** de l'entrée de `RaiseSizeList`, au lieu d'utiliser l'entrée comme clé :

```python
# RaiseSizeList = Raise75, RaisePot, Raise100, All_In
numeric_part = re.findall(r"\d+\.?\d*", raise_size)  # "Raise75" -> ["75"]
key = f"Raise{int(numeric_value * 100)}"  # -> "Raise7500"  ✗
self.configs.setdefault(key, key)  # -> configs["Raise7500"] = "Raise7500"
```

La clé attendue est `Raise75`, qui vaut `40075` en config. Le code fabrique `Raise7500`, absent de la config, puis `setdefault` l'auto-remplit avec sa propre valeur littérale. `get_filename` produit alors `Raise7500.rng` au lieu de `40075.rng`.

Reproduction vérifiée :

```
seq         [('SB', 'Raise')]
after raise [('SB', 'Raise7500')]
filename    Raise7500.rng
exists      False
get_results BB vs SB raise: []          ← doit contenir Fold/Call/Raise
```

Second défaut du même bloc : la boucle `break` sur la **première** taille de la liste sans jamais tester l'existence du fichier. Or `RaiseSizeList` est explicitement documentée en config comme une liste de tailles candidates à essayer — les arbres ne contiennent pas tous la même sizing. Il faut sonder.

Correctif validé expérimentalement (substitution directe + sondage) :

```
[('SB','Raise')]                 -> [('SB','Raise100')]                    40100.rng          exists True
[('SB','Raise'),('BB','Raise')]  -> [('SB','Raise100'),('BB','Raise100')]  40100.40100.rng    exists True
[('SB','Raise'),('BB','Call')]   -> [('SB','Raise100'),('BB','Call')]      40100.1.rng        exists True
```

### 3.2 — BLOQUANT : la vue position `SB` crashe

`tree_reader.py:152-163`, ligne « after Limp » — l'opérateur ternaire est imbriqué **dans** la valeur de `"Results"`, si bien que les colonnes ≠ BB reçoivent un `dict` là où un `list` est attendu :

```python
{
    "isInfo": False,
    "Results": self.action_processor.get_results(...)
    if column_pos == "BB"
    else {"isInfo": False, "Results": []},  # ✗ dict imbriqué
}
```

`outputframe.preprocess_results` fait ensuite `results[0][2]` → `KeyError: 0`. Vérifié :

```
File "preflop_advisor/outputframe.py", line 277, in preprocess_results
    fold_ev = results[0][2] if ... 
KeyError: 0
```

L'exception est avalée par le `except Exception: print(...)` de `gui.py:154` : l'utilisateur voit une grille vide sans message.

### 3.3 — La position SB est inaccessible depuis l'UI

`config.ini` → `PositionInactive = MP,SB`. Les boutons listés y sont désactivés **définitivement** (`position_selector.py:172`), indépendamment du nombre de joueurs. En Heads-Up, SB est l'une des deux seules positions jouables : elle est grisée. `MP` l'est aussi alors qu'elle est valide en 6-max.

### 3.4 — `preprocess_results` suppose que l'index 0 est toujours `Fold`

`outputframe.py:277-281` retire aveuglément `results[0]` et l'utilise comme référence de `fold_ev`. Or `get_results` n'ajoute une entrée que si le fichier correspondant existe : si le fichier `Fold` manque, c'est l'entrée `Call` qui est consommée comme EV de fold et supprimée de l'affichage. Les résultats doivent être adressés par nom d'action, pas par position.

### 3.5 — Grille de sortie figée à 7×8

`outputframe.py:27-30` : `RESULT_ROWS = 7`, `RESULT_COLUMNS = 8`, `TableEntry.setFixedSize(120, 80)`.

- En Heads-Up, 3 cellules sur 56 sont utilisées → une grande grille vide.
- Avec une config `Positions` de plus de 6 entrées, `self.table_entries[row][column]` lève `IndexError`.
- `setFixedSize` annule tout redimensionnement : la fenêtre est étirable, pas son contenu.

Ironie : `output_objects.TableEntry` — code mort — implémente exactement le `resizeEvent` responsive qui manque à la version utilisée.

### 3.6 — Défauts de rendu

| Fichier | Problème |
|---|---|
| `outputframe.py:86` | `background-color: 1e1e1e;` — `#` manquant, règle CSS invalide donc ignorée |
| `outputframe.py:87,92` | `color: black` / `color: grey` sur fond sombre → contraste insuffisant |
| `outputframe.py:39` | `SUIT_COLORS["s"] = black` sur fond sombre → le pique est invisible (`card_selector.py:42` utilise `white`) |
| `outputframe.py:132,136` | La coloration conditionnelle est appliquée sur `result[1]` = **fréquence** (toujours ≥ 0, donc toujours vert ou gris) alors que l'intention évidente est l'**EV** = `result[2]` |
| partout | 9 feuilles de style inline dupliquées, aucune palette centralisée |

### 3.7 — Dettes structurelles

- **`sys.path.append(...)` dans 7 modules** pour permettre des imports absolus `preflop_advisor.*` depuis l'intérieur du paquet. Des imports relatifs suppriment ce hack et le rendent robuste sous PyInstaller.
- **Cache global mutable** : `CACHE = OrderedDict()` au niveau module (`tree_reader_helpers.py:19`) — partagé entre toutes les instances, jamais invalidé au changement d'arbre, non thread-safe.
- **Double mécanisme de notification** : `Signal` déclarés (`handChanged`, `positionChanged`, `treeChanged`) *et* callbacks `update_output` passés au constructeur. Seuls les callbacks sont câblés par `MainWindow`.
- **`logging.basicConfig(level=INFO)` dans 8 modules**, avec un `logging.info` par lecture de main et par cellule → des milliers de lignes sur stderr par rafraîchissement, coût mesurable.
- **`except Exception: print(...)`** (`gui.py:151-154`) masque les erreurs métier.
- **Chemin absolu erroné** en config : `Table12=...,/Users/jdenis/Documents/GitHub/PreflopAdvisor/ranges/...` (utilisateur `jdenis`, la machine est `jde`). Cela ne fonctionne que grâce au triple fallback ajouté dans `TreeReader.__init__` et `ActionProcessor.__init__` — une rustine qui masque le vrai problème : les chemins de ranges doivent être relatifs et résolus par un unique helper.
- **Constante magique `/ 2000`** (`outputframe.py:285`) : conversion chips → bb sous la convention Monker (bb = 2000). Correcte a priori, mais non nommée, non documentée, non testée.
- **`read_hand` non caché** (`tree_reader_helpers.py:195-217`) : mélange itération `for line in f` et `f.readline()`, filtre arbitraire `len(line) < 12` qui exclut les mains PLO5 longues, comparaison par sous-chaîne au lieu d'égalité.
- **`frequency_reader_helper.py`** (299 lignes) et **`output_objects.py`** (200 lignes) : 0 % de couverture, non branchés.
- **Aucune CI** (`.github/` absent), **ruff non installé** dans l'environnement alors qu'il est configuré dans `pyproject.toml`.

---

## 4. Plan par phases

### Phase 0 — Filet de sécurité (prérequis, ~0,5 j)

Aucune correction ne doit partir sans oracle. On fige d'abord le comportement **attendu** à partir des données réelles du dépôt.

**0.1 — Fixtures d'oracle**
Créer `tests/fixtures/` avec un mini-arbre synthétique (≈ 10 fichiers `.rng`, quelques mains) reproduisant la nomenclature Monker, plus un `conftest.py` exposant :
```python
@pytest.fixture
def tree_configs()          # section [TreeReader] chargée
@pytest.fixture
def hu_tree()               # dict tree_infos pointant sur ranges/HU-100bb-with-limp
@pytest.fixture
def synthetic_tree(tmp_path)  # arbre minimal généré, valeurs connues
```

**0.2 — Tests caractérisation `get_filename`**
Table de correspondance séquence → nom de fichier, dérivée des 31 fichiers réels :
```python
@pytest.mark.parametrize("sequence,expected", [
    ([("SB", "Raise")],                              "40100.rng"),
    ([("SB", "Raise"), ("BB", "Call")],              "40100.1.rng"),
    ([("SB", "Raise"), ("BB", "Raise")],             "40100.40100.rng"),
    ([("SB", "Call"), ("BB", "Raise")],              "1.40100.rng"),
    ([("SB", "Call")],                               "1.rng"),
    ([("SB", "Fold")],                               "0.rng"),
])
```

**0.3 — Test d'inventaire (garde-fou global)**
Test qui balaie toutes les cellules de toutes les vues position pour l'arbre HU et assert qu'un **taux minimal de cellules non vides** est atteint. C'est le test qui aurait détecté le bug 3.1 :
```python
def test_hu_tree_coverage_is_non_trivial(hu_tree, tree_configs):
    filled = total = 0
    for pos in ("X", "SB", "BB"):
        for row in TreeReader("AhKs4h3s", pos, hu_tree, tree_configs).get_results():
            for cell in row:
                if not cell["isInfo"]:
                    total += 1
                    filled += bool(cell["Results"])
    assert filled / total > 0.30  # actuellement ≈ 0.03
```

**0.4 — Test de contrat de forme**
Assert que toute cellule `isInfo=False` porte un `Results` de type `list`, dont chaque élément est `[str, float, float]`. Ce test échoue aujourd'hui sur la vue SB (bug 3.2) et empêchera toute régression de type.

*Critère de sortie : 0.2 vert, 0.3 et 0.4 rouges (échecs attendus, documentés).*

---

### Phase 1 — Réparation du cœur solver (~1 j)

**1.1 — Réécrire `find_valid_raise_sizes`** (`tree_reader_helpers.py:157`)

L'entrée de `RaiseSizeList` **est** la clé de config. Substituer directement, puis sonder l'existence du fichier pour retenir la première taille réellement présente dans l'arbre :

```python
def find_valid_raise_sizes(self, full_action_sequence):
    """Substitue chaque 'Raise' générique par la première sizing de
    RaiseSizeList pour laquelle un fichier existe dans l'arbre."""
    resolved = []
    for position, action in full_action_sequence:
        if action != "Raise":
            resolved.append((position, action))
            continue
        for size_key in self.raise_size_keys:  # ["Raise75","RaisePot","Raise100","All_In"]
            candidate = resolved + [(position, size_key)]
            if self._sequence_has_descendant(candidate):
                resolved.append((position, size_key))
                break
        else:
            logging.debug("Aucune sizing valide pour %s dans %s", position, self.path)
            resolved.append((position, self.raise_size_keys[0]))
    return resolved
```

`_sequence_has_descendant` teste le préfixe : un raise intermédiaire n'a pas de fichier propre mais préfixe des fichiers descendants. Implémentation via un index des noms de fichiers construit une fois par `ActionProcessor` (`set(os.listdir(self.path))` + set des préfixes) — évite un `listdir` par cellule.

**1.2 — Supprimer la dérivation numérique** dans `__init__` (`tree_reader_helpers.py:62-73`). Remplacer par une validation stricte au démarrage : toute entrée de `RaiseSizeList` absente de la config lève une erreur explicite listant les clés disponibles, au lieu de s'auto-remplir silencieusement via `setdefault`.

**1.3 — Corriger la ligne « after Limp »** (`tree_reader.py:152-163`). Sortir le ternaire de la valeur `Results` :

```python
row = [{"isInfo": True, "Text": "after Limp"}]
for column_pos in self.position_list:
    if column_pos == "BB":
        results = self.action_processor.get_results(self.hand, [("SB", "Call"), ("BB", "Raise")], pos)
    else:
        results = []
    row.append({"isInfo": False, "Results": results})
```

**1.4 — Indexer les résultats par action** dans `preprocess_results` (`outputframe.py:272`). Chercher explicitement l'entrée `Fold` pour `fold_ev` et n'afficher que les actions non-Fold, au lieu de `results[0]` / `results[1:]`.

**1.5 — Extraire la conversion d'EV**. `CHIPS_PER_BB = 2000` en constante nommée, exposée en config `[Output] ChipsPerBB=2000`, avec un test qui vérifie qu'un fold en SB donne bien −0,5 bb sur l'arbre HU réel.

**1.6 — Durcir `read_hand`** : comparaison par égalité stricte sur la ligne strippée, suppression du filtre `len(line) < 12`, lecture par paires de lignes (même logique que `read_file_into_hash`) plutôt que mélange itérateur/`readline`.

*Critère de sortie : les tests 0.3 et 0.4 passent au vert. `get_results` pour BB vs SB-raise renvoie des entrées non vides.*

---

### Phase 2 — Non-régression et couverture (~1 j)

Objectif : **85 % de couverture sur la couche métier** (`tree_reader`, `tree_reader_helpers`, `hand_convert_helper`), avec des assertions de valeur et non de type.

**2.1 — `hand_convert_helper` (33 % → 95 %)**
Le module est pur et déterministe : c'est la cible la plus rentable. Tests paramétrés sur les trois convertisseurs, avec les invariants suivants :

| Invariant | Test |
|---|---|
| Idempotence | `convert_hand(h)` stable si réappliqué sur une main déjà normalisée |
| Permutation | toute permutation des cartes d'une main donne la même sortie |
| Isomorphisme de couleurs | `AhKh2c3c` et `AsKs2d3d` donnent la même sortie |
| Longueurs invalides | 6 cartes → retour inchangé + log d'erreur |
| Rangs/couleurs invalides | `AxKs2c3c` → retour inchangé |
| Existence | toute sortie de `convert_hand` existe comme clé dans les fichiers `.rng` réels |

Le dernier point est le plus fort : il croise le convertisseur avec les données réelles et détecterait toute dérive de format Monker.

**2.2 — `tree_reader` : lignes de jeu (74 % → 90 %)**
Chaque helper (`get_vs_first_in`, `get_4bet`, `get_vs_4bet`, `get_squeeze`, `get_vs_squeeze`) reçoit ses tests de **séquence d'actions** — on ne vérifie pas les valeurs solver, on vérifie que la ligne de jeu construite est celle attendue au sens poker :
- `get_squeeze` renvoie `[]` si moins d'un joueur entre le RFI et nous ;
- `get_vs_4bet` renvoie `[]` en UTG (pas de cold 4bet possible) ;
- `get_4bet` renvoie `[]` face à un 3bet UTG en cold ;
- symétrie : `get_vs_first_in(A, B)` non vide ⟺ `get_vs_first_in(B, A)` non vide.

Ajouter les gardes manquantes : `get_vs_4bet`, `get_4bet`, `get_squeeze`, `get_vs_squeeze` appellent `.index()` sans vérifier l'appartenance → `ValueError` possible. Tester avec une position hors arbre.

**2.3 — `tree_reader_helpers` : I/O et cache (64 % → 85 %)**
- `get_action_sequence` : remplissage des Fold intermédiaires, cas d'un joueur agissant deux fois, ordre de parole.
- Cache : identité de résultat cache activé / désactivé (`CacheSize=0` vs `100`) — test différentiel sur l'arbre HU complet.
- Éviction LRU : `CacheSize=2`, trois fichiers, vérifier l'expulsion du plus ancien.
- Fichier absent / ligne malformée / EV non numérique → dégradation propre, pas d'exception.

**2.4 — Tests GUI (pytest-qt)**
Les tests actuels appellent directement `process_button_clicked` : ils court-circuitent Qt. Passer par `qtbot` pour tester le vrai chemin d'événements :
```python
def test_selecting_four_cards_refreshes_output(qtbot, main_window):
    with qtbot.waitSignal(main_window.card_selector.handChanged, timeout=1000):
        qtbot.mouseClick(main_window.card_selector.button_list[0][0], Qt.LeftButton)
        ...
```
Plus : test de bout en bout « sélection d'une main + position SB → au moins une cellule renseignée » (couvre 3.2 et 3.3 au niveau UI), et test de non-régression sur `update_active_positions` (SB doit être cliquable en HU).

**2.5 — Verrouiller le seuil**
`--cov-fail-under=80` dans `pyproject.toml`, après exclusion des blocs `if __name__ == "__main__"` et des fonctions `test()` de démo via `[tool.coverage.report] exclude_lines`.

**2.6 — CI**
`.github/workflows/ci.yml` : matrice Python 3.10/3.11/3.12, `uv sync`, `ruff check`, `ruff format --check`, `pytest --cov` sous `xvfb-run` (Qt offscreen : `QT_QPA_PLATFORM=offscreen`).

*Critère de sortie : couverture métier ≥ 85 %, CI verte, seuil bloquant actif.*

---

### Phase 3 — Assainissement architectural (~1 j)

**3.1 — Imports relatifs.** Supprimer les 7 `sys.path.append`. `from .tree_reader_helpers import ActionProcessor`. Vérifier le build PyInstaller après coup (c'est justement ce que le hack tentait de contourner).

**3.2 — Résolution de chemins centralisée.** Un module `paths.py` :
```python
def resolve_range_folder(folder: str) -> Path:
    """Résout un dossier de ranges : absolu, relatif au projet, ou basename sous ranges/."""
```
Remplace les deux blocs de fallback dupliqués (`tree_reader.py:42-58`, `tree_reader_helpers.py:39-50`). Corriger `config.ini` pour utiliser `ranges/HU-100bb-with-limp` — chemin relatif, portable.

**3.3 — Cache par instance.** `self._cache` dans `ActionProcessor`, invalidé au changement d'arbre. Le cache global actuel conserve indéfiniment les ranges d'arbres désélectionnés (jusqu'à 10 Go de RAM selon le commentaire de config).

**3.4 — Un seul mécanisme de notification.** Garder les `Signal` Qt, supprimer les callbacks `update_output` des constructeurs. `MainWindow` connecte :
```python
self.card_selector.handChanged.connect(self.update_output_frame)
self.position_selector.positionChanged.connect(self.update_output_frame)
self.tree_selector.treeChanged.connect(self.update_output_frame)
```
Attention à la ré-entrance : `update_active_positions` appelle `process_button_clicked` qui réémet le signal → garder un flag `_updating` ou `blockSignals`.

**3.5 — Logging.** Retirer les 8 `logging.basicConfig`, un seul point de configuration dans `__main__.py`. Rétrograder les logs par-cellule et par-main de `INFO` à `DEBUG`. Ajouter `--verbose` en CLI.

**3.6 — Gestion d'erreurs.** Remplacer le `except Exception: print()` de `gui.py` par des exceptions typées (`RangeFolderNotFound`, `InvalidRaiseSizing`) et une `QStatusBar` qui affiche le message à l'utilisateur.

**3.7 — Code mort.** Supprimer `output_objects.py` (dupliqué, jamais importé) — mais **d'abord** récupérer son `resizeEvent` responsive pour la phase 4. Décider du sort de `frequency_reader_helper.py` : soit le brancher (voir 4.5), soit le déplacer sous `scripts/`.

*Critère de sortie : `ruff check` sans erreur, build PyInstaller fonctionnel, aucun `sys.path` manipulé.*

---

### Phase 4 — UI/UX (~1,5 j)

**4.1 — Grille dynamique.** `OutputFrame` reconstruit sa grille selon `len(results)` × `len(results[0])` au lieu du 7×8 figé. Suppression de `setFixedSize` au profit de `QSizePolicy.Expanding` + `resizeEvent` avec police proportionnelle (repris de `output_objects.py`). Résout à la fois l'`IndexError` sur >6 positions et la grille vide en HU.

**4.2 — Palette centralisée.** Un `theme.py` avec les tokens (fond, surface, bordure, texte primaire/secondaire, sémantique EV+/EV−/neutre, couleurs de couleurs de cartes) et une feuille QSS unique appliquée sur `QApplication`. Supprime les 9 blocs de style inline dupliqués. Corrige au passage `background-color: 1e1e1e` (`#` manquant) et `SUIT_COLORS["s"] = black` (invisible sur fond sombre).

**4.3 — Coloration sur l'EV, pas la fréquence.** `apply_style(label, result[2])` au lieu de `result[1]`. Aujourd'hui la coloration ne porte aucune information : la fréquence étant toujours ≥ 0, toute cellule non nulle est verte.

Aller plus loin qu'un tricolore : **la fréquence est l'information dominante** pour un joueur qui lit une grille. Proposition :
- fond de cellule = gradient d'intensité proportionnel à la fréquence (0 % → transparent, 100 % → saturé), couleur par action (fold gris, call bleu, raise rouge) ;
- l'EV en texte secondaire avec signe et code couleur ;
- palette validée pour le daltonisme (éviter le couple rouge/vert seul — doubler par la luminosité et le libellé d'action).

**4.4 — Lisibilité de la grille.**
- En-têtes de lignes/colonnes figés lors du scroll (`QTableView` avec header, ou `QScrollArea` + widgets d'en-tête ancrés).
- Cellule vide explicitement neutre (« — ») plutôt qu'un rectangle vide indistinguable d'un bug.
- Tooltip par cellule affichant la séquence d'actions complète et le fichier `.rng` source — précieux pour le débogage utilisateur et pour vérifier les hypothèses de sizing (ce que le README demande de faire à la main).
- Indicateur de sizing effectivement retenue par `find_valid_raise_sizes` (ex. badge « pot » / « 75 % ») : information aujourd'hui totalement invisible alors qu'elle change l'interprétation.

**4.5 — Le bouton Random.** `RandomButton` tire un nombre 0-100 et ne fait rien d'autre : il n'est connecté à aucune fréquence. L'usage attendu est la **randomisation de stratégie mixte** — comparer le tirage à la fréquence de l'action affichée. Le brancher : au tirage, surligner dans la grille l'action que le tirage désigne pour la cellule sélectionnée. Sinon, le retirer.

**4.6 — Sélecteur de position.** Corriger `PositionInactive` : supprimer `MP,SB` de la config et laisser `update_active_positions(num_players)` être la seule source de vérité. Ajouter un test de non-régression (2.4).

**4.7 — Sélecteur de cartes.** Le comportement « limite atteinte → on efface tout » (`card_selector.py:135-139`) est frustrant : un clic de trop détruit la sélection entière. Préférer un remplacement FIFO de la carte la plus ancienne, ou un feedback visuel avant reset. Ajouter aussi : les cartes déjà sélectionnées d'un autre rang ne sont pas grisées, rien n'empêche de comprendre l'état.

*Critère de sortie : grille responsive vérifiée en HU / 6-max, contraste AA vérifié, captures d'écran mises à jour dans le README.*

---

### Phase 5 — Outillage & finitions (~0,5 j)

- `ruff` installé dans l'environnement de dev (`uv sync --extra dev`), `ruff format` appliqué au dépôt.
- Pre-commit hook : `ruff check --fix` + `ruff format` + `pytest -q`.
- Nettoyer les artefacts versionnés : `frequencies.pkl`, `weight_lookup_*.pickle` (fichiers de 5 octets, vides) et `.coverage` (52 Ko) sont suivis par git — les retirer et les ajouter au `.gitignore`.
- README : documenter le format `.rng`, la sémantique de `RaiseSizeList` (nom = clé de config), et la convention `CHIPS_PER_BB`.
- Supprimer les fonctions `test()` de démo en fin de chaque module (10 occurrences) ou les déplacer sous `scripts/demos/` — elles gonflent le dénominateur de couverture et brouillent le rôle de chaque module.

---

## 5. Séquencement et effort

| Phase | Contenu | Effort | Dépend de |
|---|---|---|---|
| 0 | Oracles et tests de caractérisation | 0,5 j | — |
| 1 | Réparation du cœur solver | 1 j | 0 |
| 2 | Non-régression, couverture, CI | 1 j | 1 |
| 3 | Assainissement architectural | 1 j | 2 |
| 4 | UI/UX | 1,5 j | 3 |
| 5 | Outillage & docs | 0,5 j | — |
| | **Total** | **5,5 j** | |

Les phases 0→2 sont non négociables et doivent être livrées d'un bloc : la phase 1 sans la phase 0 reproduit exactement la situation actuelle (du code corrigé sans preuve qu'il l'est). La phase 5 peut être menée en parallèle.

---

## 6. Risques

| Risque | Probabilité | Mitigation |
|---|---|---|
| L'arbre HU fourni ne contient qu'une sizing (`40100`) — le sondage de la phase 1.1 n'est validé que sur un cas dégénéré | élevée | Générer un arbre synthétique multi-sizings en fixture (0.1) pour couvrir `Raise75`/`RaisePot`/`All_In` |
| Les hypothèses de lignes de jeu (squeeze, cold 4bet) peuvent diverger de la convention Monker | moyenne | Faire valider les tests 2.2 par un joueur/l'auteur amont avant de figer les assertions |
| `CHIPS_PER_BB = 2000` pourrait dépendre de la profondeur de stack de l'arbre | moyenne | Test croisé sur l'EV de fold en SB (attendu −0,5 bb) ; si un second arbre est disponible, comparer |
| Le passage aux imports relatifs casse le build PyInstaller | faible | Build vérifié en CI à chaque phase (`scripts/build.py` existe déjà) |
| La refonte de la grille (4.1) casse la disposition attendue par les captures du README | faible | Régénérer les captures en fin de phase 4 |

---

## 7. Ordre d'attaque recommandé

1. **Aujourd'hui** — Phase 0 complète, puis 1.1 + 1.3. Ces deux correctifs seuls font passer l'application de « inutilisable » à « fonctionnelle » ; le reste est de la consolidation.
2. Vérifier manuellement dans l'UI qu'une grille HU complète s'affiche pour `SB` et `BB`.
3. Enchaîner 1.4 → 1.6, puis la phase 2 en entier avant tout refactoring.
