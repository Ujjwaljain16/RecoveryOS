import pandas as pd
from simulator.run import build_simulator
from simulator.episodes.generator import PaymentGenerator
from models.recovery.train import _run_leakage_gate
import sys

def main():
    print("[LeakageCheck] Generating dataset with independent seed 2026...")
    # Generate data
    from simulator.episodes.generator import EpisodeGenerator
    gen, merchants, customers, manifest = build_simulator(seed=2026, customer_count=2000)
    
    ep_gen = EpisodeGenerator(
        payment_generator=gen,
        id_gen=gen.id_gen,
        rng=gen.rng,
        clock=gen.clock,
        merchants=merchants,
        customers=customers,
        scenarios=gen.scenarios,
        noise_pipeline=gen.noise_pipeline,
        latent_function=gen.latent_function,
    )
    result = ep_gen.generate_episodes(15000, manifest.simulation_id, split_name="test_leak")
    episodes = result.episodes
    
    # Extract features matching the ones used in training
    from simulator.dataset.schema import VISIBLE_FEATURE_COLUMNS
    
    data = []
    labels = []
    for ep in episodes:
        feats = ep
        row = {c: getattr(feats, c) for c in VISIBLE_FEATURE_COLUMNS if hasattr(feats, c)}
        data.append(row)
        labels.append(ep.actual_recovered)
        
    df = pd.DataFrame(data)
    y = pd.Series(labels).astype(int).values
    
    # Split into mock train/val just for the gate function
    n_train = 10000
    feat_train = df.iloc[:n_train]
    y_train = y[:n_train]
    feat_val = df.iloc[n_train:]
    y_val = y[n_train:]
    
    from models.recovery.features import FeatureTransformer
    transformer = FeatureTransformer()
    X_train = transformer.fit_transform(feat_train)
    X_val = transformer.transform(feat_val)
    
    print("[LeakageCheck] Running leakage gate...")
    try:
        _run_leakage_gate(X_train, y_train, X_val, y_val)
        print("[LeakageCheck] PASS! The leakage gate passes on an independent seed.")
    except RuntimeError as e:
        print(f"[LeakageCheck] FAILED: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
