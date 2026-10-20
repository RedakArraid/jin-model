# JIN runtime 5.16.4 — stacked dual references and charges

This patch hardens native ERP tables in which several logical order lines are
stacked inside one physical PDF row. When both `Référence` and `Ref Four` are
printed, JIN now treats the first value as the buyer's internal code and the
second as the supplier article reference. The supplier reference becomes the
primary material/article identifier and the internal code remains available in
`customer_material_number`.

The parser also segments wrapped designations, removes routing/contact preamble
text, and aligns missing supplier cells by commercial row type. Consequently a
blank supplier cell on a freight row no longer shifts the following product
reference.

Commercial-charge recognition now includes `PORT/ACHATS` and compact DEEE
codes such as `DEEE7.69`. These rows are emitted as shipping or environmental
charges rather than products; environmental fees remain attached to the
preceding product.

The post-fix replay covered the 150 documents from the discovery campaign:
all 150 API calls succeeded, severe material-reference classifications fell
from one to zero, and the 14 documents without extracted lines remained the
same. The only line-count change was the expected removal of the three charge
rows from product lines.
