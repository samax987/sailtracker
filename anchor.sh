#!/bin/bash
# Toggle mode ancre/navigation pour InReach
ENV_FILE="/var/www/sailtracker/.env"
CURRENT=$(grep "INREACH_MODE" "$ENV_FILE" | cut -d= -f2)

if [ "$1" = "on" ] || ([ -z "$1" ] && [ "$CURRENT" != "anchor" ]); then
    sed -i "s/INREACH_MODE=.*/INREACH_MODE=anchor/" "$ENV_FILE"
    echo "⚓ Mode ANCRE activé — collecte InReach suspendue"
elif [ "$1" = "off" ] || [ -z "$1" ]; then
    sed -i "s/INREACH_MODE=.*/INREACH_MODE=sailing/" "$ENV_FILE"
    echo "⛵ Mode NAVIGATION activé — collecte InReach reprise"
fi

echo "Statut actuel : $(grep INREACH_MODE $ENV_FILE)"
