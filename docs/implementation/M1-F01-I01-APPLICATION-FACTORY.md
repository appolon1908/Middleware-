# M1-F01-I01 — Application factory implementation

Task: MIDDLEWARE_:M1-F01-I01
Design base: 1200564005e236fa3a585849b94f47981c9933aa

The implementation uses the existing canonical app.application.create_app; no second primary-runtime factory is introduced.

Implementation proof covers profile-driven registry composition, canonical application state, fail-closed configuration, effect-free construction before lifespan, runtime construction and cleanup, injected-runtime ownership, singular request-guard and health authority, duplicate route rejection, and entrypoint delegation.

The task performs no deployment, provider effect, database migration, or production mutation.
