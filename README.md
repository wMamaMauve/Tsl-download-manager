# TSL download manager

Télécharge automatiquement tous les niveaux de **The Shitty List** ([TSL](https://tsl.pages.dev/list/tsl) et [TSL+](https://tsl.pages.dev/list/tslplus)) en fichiers **`.gmd`**, détecte quand un niveau est mis à jour par son créateur et envoie le tout dans **Nextcloud**.

Tout est en bash : il faut seulement `curl`, `awk`, `sed`, `od` et `sha256sum`, présents sur quasiment tous les Linux. Python n'est pas nécessaire.

## Installation (sur le serveur, en root)

```bash
git clone https://github.com/wmamamauve/tsl-download-manager.git
cd tsl-download-manager
./install.sh            # passage automatique chaque nuit vers 4 h (./install.sh 13 pour 13 h)
nano /etc/tsl-sync.conf # renseigne NC_URL, NC_USER, NC_PASS
/opt/tsl-sync/tsl-sync.sh   # premier lancement à la main
```

Le premier lancement télécharge environ 630 niveaux avec une pause de 2 s entre chaque requête, ce qui prend une bonne demi-heure. Les passages suivants ne téléchargent que ce qui a changé.

**Mot de passe Nextcloud** : utilise un *mot de passe d'application* (Nextcloud → Paramètres → Sécurité → « Créer un mot de passe d'application »), pas ton vrai mot de passe. Le fichier `/etc/tsl-sync.conf` n'est lisible que par root.

## Stockage local puis envoi groupé

Les fichiers sont d'abord enregistrés sur le serveur dans `/var/lib/tsl-sync/fichiers/`, puis **tout est envoyé dans Nextcloud en une fois à la fin du passage**. Si Nextcloud est injoignable, les fichiers restent dans la file d'attente (`/var/lib/tsl-sync/a_envoyer.tsv`) et repartent au passage suivant. Pour envoyer au fur et à mesure à la place, mets `UPLOAD_AT_END=0` dans la config.

## Ce que tu obtiens dans Nextcloud

```
Geometry Dash/TSL/
├── tsl/
│   ├── Shitty NETWORK (132801785).gmd
│   ├── ...
│   └── index.tsv            ← classement actuel (position, id, nom, auteur, fichier)
├── tslplus/
│   └── ...
├── 2026-11-10/               ← un dossier par jour de passage :
│   ├── tsl/...gmd           ←   niveaux nouveaux ou modifiés ce jour-là
│   └── changements.log      ←   journal du jour ("Aucun changement." sinon)
├── historique/
│   └── Shitty NETWORK (132801785)/
│       └── 2026-11-09_v1.gmd   ← anciennes versions des niveaux modifiés
└── changements.log          ← journal de tout ce qui a changé
```

Les `.gmd` s'importent dans le jeu avec **GDShare** (via Geode) : ils arrivent dans tes niveaux de l'éditeur.

Les dossiers du jour sont des copies faites directement dans Nextcloud (rien n'est renvoyé depuis le serveur). Pour les désactiver : `DAILY=0` dans la config.

## Bot Discord

Le bot publie dans un salon tout ce qui change (niveau modifié, infos modifiées, supprimé, réapparu, entrée/sortie de liste) et répond aux commandes :

| Commande | Effet |
|---|---|
| `/niveau <nom ou id>` | infos du niveau, derniers changements, lien GDBrowser **et le fichier .gmd en pièce jointe** |
| `/changements [jours]` | ce qui a changé ces derniers jours |
| `/liste <tsl\|tslplus> [page]` | le classement |
| `/statut` | résultat du dernier passage |
| `/verifier` | (admins) lance un passage maintenant |

Installation : `./install-bot.sh` (installe Python et discord.py), puis renseigne `DISCORD_BOT_TOKEN` et `DISCORD_CHANNEL_ID` dans `/etc/tsl-sync.conf` (la marche à suivre est dans le fichier) et `systemctl restart tsl-bot`. Journal : `journalctl -u tsl-bot -f`.

## Détection des changements

À chaque passage, le script :

1. relit les deux listes sur tsl.pages.dev ;
2. note dans `changements.log` les **entrées**, **sorties** et **changements de classement** ;
3. re-télécharge **chaque** niveau et compare tout son contenu (empreinte SHA-256 des données) — le numéro de version n'est pas fiable ;
4. compare aussi les infos (nom, description, musique, étoiles, difficulté, featured/epic…) et signale lesquelles ont changé ;
   un niveau qui disparaît des serveurs GD est signalé **SUPPRIMÉ** (sa dernière copie est gardée), puis **RÉAPPARU** s'il revient ;
5. si le contenu a vraiment changé, l'ancienne version part dans `historique/` et la nouvelle remplace le fichier (**MODIFIÉ** dans le journal).

Exemple de journal :

```
2026-11-10 04:21:07 [tsl] ENTRÉE dans la liste : #12 Shitty Truc (123456789)
2026-11-10 04:21:07 [tsl] CLASSEMENT : Shitty Nhelv (106481744) #3 -> #4
2026-11-10 04:23:40 [tsl] MODIFIÉ : Shitty NETWORK (132801785) v1 -> v2 (ancienne version dans historique/...)
```

## Commandes utiles

| Action | Commande |
|---|---|
| Lancer un passage maintenant | `/opt/tsl-sync/tsl-sync.sh` |
| Renvoyer **tous** les fichiers locaux vers Nextcloud | `RESYNC=1 /opt/tsl-sync/tsl-sync.sh` |
| Voir le journal (systemd) | `journalctl -u tsl-sync` |
| Prochain passage prévu | `systemctl list-timers tsl-sync.timer` |
| Mettre à jour le script | `git pull && ./install.sh` |

## Remarques

- Les niveaux présents dans les deux listes ne sont stockés qu'une fois, dans `tsl/` ; l'index de `tslplus` pointe vers ce fichier.
- Un niveau supprimé ou rendu privé sur les serveurs GD est signalé comme « en échec » ; son dernier `.gmd` reste conservé.
- Les serveurs de Geometry Dash bloquent les IP de datacenter (Cloudflare, erreur 1005). Le script doit donc tourner sur une machine qui y a accès. Teste avec :
  `curl -s -A "" -X POST https://www.boomlings.com/database/downloadGJLevel22.php -d "levelID=132801785&secret=Wmfd2893gb7" | head -c 100`
