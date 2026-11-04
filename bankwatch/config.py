"""Configuration via variables d'environnement / fichier .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="BW_", extra="ignore")

    # Fournisseur open banking : "enablebanking" (défaut) ou "gocardless" (anciens comptes uniquement)
    provider: str = "enablebanking"

    # Enable Banking (https://enablebanking.com/cp/applications), mode restricted
    eb_app_id: str = ""
    eb_key_path: Path = Path("data/enablebanking.pem")
    eb_base_url: str = "https://api.enablebanking.com"
    eb_redirect_url: str = "https://bankwatch.example.com/callback"
    eb_access_days: int = 180
    eb_daily_access_limit: int = 4
    sync_schedule: str = "07:20,13:20,20:20"  # heures des passages du timer (affichage dashboard)  # DSP2 : accès sans présence de l'utilisateur, par compte et par jour

    # GoCardless Bank Account Data (inscriptions fermées : anciens comptes uniquement)
    gc_secret_id: str = ""
    gc_secret_key: str = ""
    gc_base_url: str = "https://bankaccountdata.gocardless.com/api/v2"
    gc_redirect_url: str = "http://localhost:8787/callback"
    gc_country: str = "FR"
    gc_history_days: int = 180  # max historique demandé à la banque
    gc_access_days: int = 180  # durée de validité du consentement demandée

    # Alerting Discord : bot (token + salon), comme les autres projets du Pi ; webhook en option
    discord_channel_id: str = ""
    discord_bot_token: str = ""
    # fichier .env partagé d'où lire DISCORD_BOT_TOKEN (ex: /home/pi/discord_bridge/.env)
    discord_token_file: str = ""
    discord_webhook_url: str = ""
    discord_username: str = "BankWatch"

    # Stockage
    db_path: Path = Path("data/bankwatch.db")

    # Seuils de détection
    dup_window_days: int = 3  # fenêtre de recherche de doublons
    dup_min_amount: float = 5.0  # en dessous, on ignore (cafés, etc.)
    unusual_zscore: float = 3.0  # z-score pour « montant inhabituel » chez un marchand connu (et ≥ 2× la moyenne)
    unusual_min_history: int = 4  # nb de transactions mini pour calculer un z-score
    new_merchant_amount: float = 150.0  # nouveau marchand + montant >= seuil -> alerte
    big_amount: float = 500.0  # tout débit >= seuil -> info
    recurring_tolerance_pct: float = 10.0  # variation de montant d'un prélèvement récurrent
    overspend_pct: float = 30.0  # dépassement vs moyenne 3 mois glissants
    overspend_min_amount: float = 100.0  # ignore les catégories trop petites
    weekly_report_weekday: int = 0  # 0 = lundi

    # Tes noms tels qu'ils apparaissent dans les libellés : un virement à ton nom = virement interne
    # (exclu des dépenses et des alertes « inhabituel »). Plusieurs noms séparés par des virgules :
    # BW_OWNER_NAMES=alice martin, karakuri studio
    owner_names: str = ""

    @property
    def owner_list(self) -> list[str]:
        return [n.strip() for n in self.owner_names.split(",") if n.strip()]

    # Budgets mensuels optionnels par catégorie, ex: BW_BUDGETS='{"courses": 400, "restos": 150}'
    budgets: dict[str, float] = Field(default_factory=dict)

    # Dashboard
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    # Authentification HTTP Basic du dashboard (obligatoire dès qu'il est exposé hors localhost)
    dashboard_user: str = "admin"
    dashboard_password: str = ""
    secret_key: str = ""  # signature des cookies de session (sinon data/secret.key généré)
    dashboard_max_failures: int = 10  # échecs avant blocage temporaire de l'IP
    dashboard_lockout_minutes: int = 15


settings = Settings()
