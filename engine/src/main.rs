use clap::{Parser, Subcommand};
use std::io::{self, Read};

mod ensemble;
mod landmask;
mod geo;
mod optimize;
mod polar;
mod route;
mod score;
mod types;

#[derive(Parser)]
#[command(
    name = "sailtracker-engine",
    version = "1.0.0",
    about = "Moteur de calcul haute performance pour SailTracker"
)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Vitesse bateau pour un TWA/TWS donné (entrée JSON via stdin)
    Polar {
        /// Chemin vers le fichier CSV de polaires
        #[arg(long)]
        polars: String,
    },
    /// Calcul ETA d'une route avec polaires + vent + courants
    Route {
        /// Chemin vers le fichier CSV de polaires
        #[arg(long)]
        polars: String,
    },
    /// Routage par isochrones — recherche de la route optimale
    Optimize {
        /// Chemin vers le fichier CSV de polaires
        #[arg(long)]
        polars: String,
    },
    /// Scores de confiance et confort pour N dates de départ (parallèle)
    Score {
        /// Chemin vers le fichier CSV de polaires
        #[arg(long)]
        polars: String,
    },
    /// Statistiques sur les membres d'ensemble (mean, std, percentiles)
    Ensemble,
    /// Informations sur le binaire (version, modules, date de compilation)
    Version,
}

fn read_stdin() -> String {
    let mut buf = String::new();
    io::stdin()
        .read_to_string(&mut buf)
        .expect("Impossible de lire stdin");
    buf
}

fn main() {
    let cli = Cli::parse();

    let result = match &cli.command {
        Commands::Polar { polars } => polar::run(read_stdin(), polars),
        Commands::Route { polars } => route::run(read_stdin(), polars),
        Commands::Optimize { polars } => optimize::run(read_stdin(), polars),
        Commands::Score { polars } => score::run(read_stdin(), polars),
        Commands::Ensemble => ensemble::run(read_stdin()),
        Commands::Version => {
            let info = serde_json::json!({
                "version": env!("CARGO_PKG_VERSION"),
                "name": env!("CARGO_PKG_NAME"),
                "compiled_at": env!("SAILTRACKER_BUILD_DATE"),
                "modules": ["polar", "route", "optimize", "score", "ensemble", "version"],
                "rust_edition": "2021"
            });
            Ok(info.to_string())
        }
    };

    match result {
        Ok(output) => println!("{}", output),
        Err(e) => {
            eprintln!("{{\"error\": \"{}\"}}", e.replace('"', "'"));
            std::process::exit(1);
        }
    }
}
