# V5.5 - Weak Field Learning CPU

V5.5 adds a CPU token/field suggester trained from deterministic high-precision weak labels on the real PDF corpus.

## Training split

- 1,572 train PDFs
- 396 validation PDFs held out
- 1,468 train PDFs with native first-page text used for weight fitting
- 372 validation PDFs with native first-page text used for weak-label agreement scoring
- 104 train scans and 24 validation scans are not used as supervised labels; runtime handles them with Tesseract OCR fallback.

## Qualification

The reported metrics measure **agreement with deterministic weak labels**, not human-reviewed ground truth. The model therefore never overwrites core JIN fields and every suggestion is emitted with `requires_review=true`.

Weak-label holdout agreement:

- token accuracy: **98.97%**
- macro-F1: **97.82%**
- address number F1: **99.64%**
- street type F1: **99.35%**
- street name F1: **99.41%**
- postal code F1: **98.35%**
- city F1: **97.78%**
- CEDEX F1: **99.43%**
- BP F1: **98.01%**
- CS F1: **100%**

These values are not claimed as real-world field accuracy until reviewed annotations exist.

## Runtime

Mount:

```text
data/learning/jin-field-weak-router-v2-cpu.joblib
```

Endpoint:

```bash
curl -F "file=@commande.pdf" http://localhost:8080/api/learning/field-route
```

`/extract` adds a non-destructive `weak_field_suggestions` block when the model is mounted.

Native PDF word geometry is preferred. If page 1 is image-only, Tesseract OCR is used on CPU.

Structural guardrails prevent common multi-column errors: street-name spans must stay spatially linked to a street type, and city/CEDEX spans must stay linked to a postal code.

## Artifact

```text
jin-field-weak-router-v2-cpu.joblib
SHA-256: 1eae5cfe609c286d777a7ccc83dbaf233a917a989cf1408b681abaeedf467517
Size: 8,674,640 bytes
```
