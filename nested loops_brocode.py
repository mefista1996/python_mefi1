#nested loops - innder loop which will finish all
# of it's iterations before finishing one iteration of the outer loops

rows = int(input("Rows?: "))
col = int(input("Col?: "))
sym = (input("Sym?: "))

for i in range(rows):
    for j in range(col):
        print(sym, end="")
    print()  #це потрібно для зовнішнього циклу