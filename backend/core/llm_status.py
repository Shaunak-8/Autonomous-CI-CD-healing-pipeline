"""Check provider availability before attempting a batch of code fixes."""
from groq import AuthenticationError, APIConnectionError, APITimeoutError


def check_groq(client, model):
    if client is None:
        return {'ready': False, 'provider': 'groq', 'model': model,
                'reason': 'GROQ_API_KEY is not configured. Add an active key to backend/.env.'}
    try:
        models = {item.id for item in client.models.list().data}
        if model not in models:
            return {'ready': False, 'provider': 'groq', 'model': model,
                    'reason': f'The configured Groq model {model} is unavailable. Set GROQ_MODEL to an enabled model.',
                    'available_models': sorted(models)}
        return {'ready': True, 'provider': 'groq', 'model': model, 'reason': ''}
    except AuthenticationError:
        return {'ready': False, 'provider': 'groq', 'model': model,
                'reason': 'Groq rejected GROQ_API_KEY (invalid or expired). Replace it in backend/.env and restart the backend.'}
    except (APIConnectionError, APITimeoutError):
        return {'ready': False, 'provider': 'groq', 'model': model,
                'reason': 'Groq could not be reached. Check the network and retry.'}
    except Exception as exc:
        return {'ready': False, 'provider': 'groq', 'model': model,
                'reason': f'Groq readiness check failed ({type(exc).__name__}). Check provider access and retry.'}
