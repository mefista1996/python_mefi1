#index operator [] = they give access to sequence elements (str,lists,turples)

name = "victoria"
if(name.index("i")) == 0:
    print("wow")
else:
    print("hold up")

#alternative

if(name[0].islower()):
    print("wow")
else:
    print("hold up")