# V5.5.1 - Geometry hardening

V5.5.1 fixes a structural limitation exposed by nine real ISERBA/UNICIA/GAZ SERVICE RAPIDE purchase orders.

The PDF stores visually aligned content in separate internal blocks. The former token model grouped by PDF block, so values such as order number, date, delivery postal code/city and Total HT could be invisible to the same-line context.

## Structural fix

The runtime now rebuilds **visual lines** from word Y-coordinates independently of the PDF block IDs. A geometry layer then complements the statistical token model.

Guardrails:

- postal codes must be raw `^\d{5}$` values in the French numeric range; `215,00`, `469,30` etc. cannot become postal codes;
- street types must belong to the road-type vocabulary; contact surnames such as `RODAS` cannot become a street type;
- postal code and city may be joined across adjacent PDF blocks/lines when their geometry is coherent;
- order number, order date and Total HT are recovered from explicit visual anchors;
- address candidates are assigned `supplier`, `ship_to` and `bill_to` roles from address-section anchors;
- ZI/ZA/ZAC/ZAE, building and BP/CS/TSA lines are attached to the relevant address block.

The learned V5.5 token model remains active. Geometry is a complementary high-precision structural layer, not a replacement.

## Nine-order regression

Before V5.5.1:

- order number: 0/9
- order date: 0/9
- Total HT: 0/9
- delivery postal code/city: 0/9
- two false `RODAS DE` address candidates
- seven documents with monetary amounts classified as postal codes

After V5.5.1:

- order number: **9/9**
- order date: **9/9**
- Total HT: **9/9**
- supplier street / postal / city: **9/9**
- delivery street / postal / city: **9/9**
- billing street / postal / city / CEDEX / CS: **9/9**
- false address candidates: **0**
- monetary postal-code false positives: **0**

This is a regression result on the nine supplied documents, not a universal accuracy guarantee. All weak-field suggestions remain `requires_review=true`.

## Training-data hardening

The weak-label training script now checks the **raw token** for five digits rather than its punctuation-stripped normalization. This prevents values such as `215,00` from becoming `21500` postal-code labels during future retraining.
