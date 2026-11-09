import aws_cdk as cdk

from doclens.doclens_stack import DocLensStack


app = cdk.App()
DocLensStack(app, "DocLensStack")
app.synth()
