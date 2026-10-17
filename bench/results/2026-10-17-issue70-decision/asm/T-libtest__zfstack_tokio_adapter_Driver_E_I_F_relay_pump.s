0000000000271590 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump>:
  271590:	push   %rbp
  271591:	push   %r15
  271593:	push   %r14
  271595:	push   %r13
  271597:	push   %r12
  271599:	push   %rbx
  27159a:	sub    $0x648,%rsp
  2715a1:	mov    %rcx,0x40(%rsp)
  2715a6:	mov    %rdx,0x8(%rsp)
  2715ab:	cmpq   $0x0,0x9e8(%rdi)
  2715b3:	je     271ccb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x73b>
  2715b9:	mov    %rsi,%rbx
  2715bc:	mov    %rdi,%rbp
  2715bf:	mov    0x9f0(%rdi),%rdi
  2715c6:	mov    0x9f8(%rbp),%rsi
  2715cd:	mov    %rbx,%rdx
  2715d0:	call   139b80 <core::hash::BuildHasher::hash_one>
  2715d5:	mov    0x9d0(%rbp),%rcx
  2715dc:	mov    0x9d8(%rbp),%rdx
  2715e3:	mov    %rdx,%rsi
  2715e6:	and    %rax,%rsi
  2715e9:	shr    $0x39,%rax
  2715ed:	movd   %eax,%xmm0
  2715f1:	punpcklbw %xmm0,%xmm0
  2715f5:	pshuflw $0x0,%xmm0,%xmm0
  2715fa:	pshufd $0x0,%xmm0,%xmm0
  2715ff:	lea    -0x78(%rcx),%rax
  271603:	xor    %edi,%edi
  271605:	pcmpeqd %xmm1,%xmm1
  271609:	movdqu (%rcx,%rsi,1),%xmm2
  27160e:	movdqa %xmm2,%xmm3
  271612:	pcmpeqb %xmm0,%xmm3
  271616:	pmovmskb %xmm3,%r8d
  27161b:	test   %r8d,%r8d
  27161e:	je     271645 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb5>
  271620:	tzcnt  %r8d,%r9d
  271625:	add    %rsi,%r9
  271628:	and    %rdx,%r9
  27162b:	neg    %r9
  27162e:	imul   $0x78,%r9,%r9
  271632:	cmp    %rbx,(%rax,%r9,1)
  271636:	je     271667 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd7>
  271638:	lea    -0x1(%r8),%r9d
  27163c:	and    %r8w,%r9w
  271640:	mov    %r9d,%r8d
  271643:	jne    271620 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x90>
  271645:	pcmpeqb %xmm1,%xmm2
  271649:	pmovmskb %xmm2,%r8d
  27164e:	test   %r8d,%r8d
  271651:	jne    271ccb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x73b>
  271657:	add    %rdi,%rsi
  27165a:	add    $0x10,%rsi
  27165e:	add    $0x10,%rdi
  271662:	and    %rdx,%rsi
  271665:	jmp    271609 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x79>
  271667:	lea    (%rcx,%r9,1),%r14
  27166b:	lea    (%rcx,%r9,1),%rax
  27166f:	add    $0xffffffffffffff90,%rax
  271673:	mov    %rax,0xe8(%rsp)
  27167b:	mov    0x8(%rsp),%rax
  271680:	lea    0x190(%rax),%rax
  271687:	mov    %rax,0x148(%rsp)
  27168f:	movq   $0x0,(%rsp)
  271697:	lea    0x348(%rbp),%rax
  27169e:	mov    %rax,0x88(%rsp)
  2716a6:	lea    0x6f8(%rbp),%rax
  2716ad:	mov    %rax,0x30(%rsp)
  2716b2:	lea    -0x40(%rcx,%r9,1),%rax
  2716b7:	mov    %rax,0x38(%rsp)
  2716bc:	mov    %ebx,%eax
  2716be:	mov    %rbx,0x28(%rsp)
  2716c3:	shr    $0x20,%rbx
  2716c7:	mov    %rbx,0x60(%rsp)
  2716cc:	lea    0x238(%rsp),%rbx
  2716d4:	mov    %rax,0x68(%rsp)
  2716d9:	imul   $0xb8,%rax,%rax
  2716e0:	mov    %rax,0x50(%rsp)
  2716e5:	mov    %rbp,0x18(%rsp)
  2716ea:	jmp    2716fb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x16b>
  2716ec:	nopl   0x0(%rax)
  2716f0:	mov    %rbp,%rdi
  2716f3:	call   *%r13
  2716f6:	mov    0x18(%rsp),%rbp
  2716fb:	mov    -0x20(%r14),%rax
  2716ff:	cmp    $0x7ffff,%rax
  271705:	ja     271890 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  27170b:	mov    %r12,%rcx
  27170e:	xchg   %ax,%ax
  271710:	cmpb   $0x1,-0x8(%r14)
  271715:	je     271887 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2f7>
  27171b:	mov    $0x303,%r12d
  271721:	mov    0x68(%rsp),%rdx
  271726:	cmp    0x4a0(%rbp),%rdx
  27172d:	jae    271850 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  271733:	mov    0x498(%rbp),%rdx
  27173a:	mov    0x60(%rsp),%rsi
  27173f:	mov    0x50(%rsp),%rdi
  271744:	cmp    %esi,0xb0(%rdx,%rdi,1)
  27174b:	jne    271850 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  271751:	add    %rdi,%rdx
  271754:	cmpw   $0x2,(%rdx)
  271758:	jne    271840 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2b0>
  27175e:	mov    0xa8(%rdx),%rsi
  271765:	test   %rsi,%rsi
  271768:	je     271850 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  27176e:	mov    $0x80000,%edx
  271773:	sub    %rax,%rdx
  271776:	lea    0x230(%rsp),%rdi
  27177e:	mov    0x88(%rsp),%rcx
  271786:	mov    0x30(%rsp),%r8
  27178b:	mov    0x40(%rsp),%r9
  271790:	call   1b98c0 <zfstack::conn::Conn::read_chunk_for_adapter>
  271795:	mov    %rbp,%rdi
  271798:	mov    0x28(%rsp),%rsi
  27179d:	call   1cb1e0 <zfstack::shard::Shard::after_conn_change>
  2717a2:	mov    0x230(%rsp),%r15
  2717aa:	mov    0x238(%rsp),%r12
  2717b2:	test   %r15,%r15
  2717b5:	je     271850 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2c0>
  2717bb:	mov    0x240(%rsp),%r13
  2717c3:	mov    0x248(%rsp),%rbp
  2717cb:	add    %r13,-0x20(%r14)
  2717cf:	mov    -0x40(%r14),%rcx
  2717d3:	mov    -0x28(%r14),%rax
  2717d7:	cmp    %rcx,%rax
  2717da:	jne    2717ee <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25e>
  2717dc:	mov    0x38(%rsp),%rdi
  2717e1:	call   1489d0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  2717e6:	mov    -0x40(%r14),%rcx
  2717ea:	mov    -0x28(%r14),%rax
  2717ee:	mov    -0x30(%r14),%rdx
  2717f2:	add    %rax,%rdx
  2717f5:	cmp    %rcx,%rdx
  2717f8:	mov    $0x0,%esi
  2717fd:	cmovb  %rsi,%rcx
  271801:	sub    %rcx,%rdx
  271804:	mov    -0x38(%r14),%rcx
  271808:	shl    $0x5,%rdx
  27180c:	mov    %r15,(%rcx,%rdx,1)
  271810:	mov    %r12,0x8(%rcx,%rdx,1)
  271815:	mov    %r13,0x10(%rcx,%rdx,1)
  27181a:	mov    %rbp,0x18(%rcx,%rdx,1)
  27181f:	inc    %rax
  271822:	mov    %rax,-0x28(%r14)
  271826:	mov    0x18(%rsp),%rbp
  27182b:	mov    -0x20(%r14),%rax
  27182f:	mov    %r12,%rcx
  271832:	cmp    $0x80000,%rax
  271838:	jb     271710 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x180>
  27183e:	jmp    271890 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  271840:	and    $0xffffffffffffff00,%rcx
  271847:	or     $0x2,%rcx
  27184b:	mov    %rcx,%r12
  27184e:	xchg   %ax,%ax
  271850:	cmp    $0x2,%r12b
  271854:	je     27186d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2dd>
  271856:	movzbl %r12b,%eax
  27185a:	cmp    $0x3,%eax
  27185d:	jne    271890 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  27185f:	mov    %r12d,%eax
  271862:	shr    $0x8,%eax
  271865:	test   %al,%al
  271867:	jne    271cd2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x742>
  27186d:	movb   $0x1,-0x8(%r14)
  271872:	mov    -0x20(%r14),%rax
  271876:	mov    %r12,%rcx
  271879:	cmp    $0x80000,%rax
  27187f:	jb     271710 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x180>
  271885:	jmp    271890 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x300>
  271887:	mov    %rcx,%r12
  27188a:	nopw   0x0(%rax,%rax,1)
  271890:	cmpq   $0x0,-0x28(%r14)
  271895:	je     272024 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xa94>
  27189b:	mov    $0x38,%eax
  2718a0:	movq   $0x1,0x1f8(%rsp,%rax,1)
  2718ac:	movq   $0x0,0x200(%rsp,%rax,1)
  2718b8:	movq   $0x1,0x208(%rsp,%rax,1)
  2718c4:	movq   $0x0,0x210(%rsp,%rax,1)
  2718d0:	movq   $0x1,0x218(%rsp,%rax,1)
  2718dc:	movq   $0x0,0x220(%rsp,%rax,1)
  2718e8:	movq   $0x1,0x228(%rsp,%rax,1)
  2718f4:	movq   $0x0,0x230(%rsp,%rax,1)
  271900:	add    $0x40,%rax
  271904:	cmp    $0x438,%rax
  27190a:	jne    2718a0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x310>
  27190c:	mov    -0x28(%r14),%rdx
  271910:	test   %rdx,%rdx
  271913:	je     271950 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c0>
  271915:	mov    -0x40(%r14),%rcx
  271919:	mov    -0x30(%r14),%rax
  27191d:	cmp    %rcx,%rax
  271920:	mov    $0x0,%esi
  271925:	cmovae %rcx,%rsi
  271929:	sub    %rsi,%rax
  27192c:	mov    %rcx,%rdi
  27192f:	sub    %rax,%rdi
  271932:	mov    %rdx,%rsi
  271935:	sub    %rdi,%rsi
  271938:	ja     271956 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c6>
  27193a:	add    %rax,%rdx
  27193d:	mov    %rdx,%rcx
  271940:	jmp    271954 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3c4>
  271942:	data16 data16 data16 data16 cs nopw 0x0(%rax,%rax,1)
  271950:	xor    %eax,%eax
  271952:	xor    %ecx,%ecx
  271954:	xor    %esi,%esi
  271956:	mov    -0x38(%r14),%rdi
  27195a:	shl    $0x5,%rax
  27195e:	add    %rdi,%rax
  271961:	shl    $0x5,%rcx
  271965:	add    %rdi,%rcx
  271968:	shl    $0x5,%rsi
  27196c:	add    %rdi,%rsi
  27196f:	mov    %rbx,%r8
  271972:	xor    %edx,%edx
  271974:	jmp    2719a8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x418>
  271976:	cs nopw 0x0(%rax,%rax,1)
  271980:	mov    %rcx,%r9
  271983:	mov    %rsi,%rcx
  271986:	mov    %rcx,%rsi
  271989:	movdqu 0x8(%rax),%xmm0
  27198e:	add    $0x20,%rax
  271992:	movdqu %xmm0,-0x8(%r8)
  271998:	inc    %rdx
  27199b:	add    $0x10,%r8
  27199f:	mov    %r9,%rcx
  2719a2:	cmp    $0x40,%rdx
  2719a6:	je     2719d0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x440>
  2719a8:	cmp    %rcx,%rax
  2719ab:	jne    271980 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3f0>
  2719ad:	test   %rdi,%rdi
  2719b0:	je     2719c0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x430>
  2719b2:	mov    %rdi,%rax
  2719b5:	mov    %rsi,%r9
  2719b8:	cmp    %rsi,%rdi
  2719bb:	mov    %rcx,%rdi
  2719be:	jne    271986 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x3f6>
  2719c0:	cmp    $0x41,%rdx
  2719c4:	jb     2719d5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x445>
  2719c6:	jmp    273765 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21d5>
  2719cb:	nopl   0x0(%rax,%rax,1)
  2719d0:	mov    $0x40,%edx
  2719d5:	mov    0xe8(%rsp),%rdi
  2719dd:	lea    0x230(%rsp),%rsi
  2719e5:	call   3bc8f0 <tokio::net::tcp::stream::TcpStream::try_write_vectored>
  2719ea:	mov    %rdx,%r13
  2719ed:	test   %rax,%rax
  2719f0:	je     271a60 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x4d0>
  2719f2:	mov    %r13,%rdi
  2719f5:	call   459e0 <std::io::error::Error::kind>
  2719fa:	cmp    $0xd,%al
  2719fc:	jne    271b90 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x600>
  271a02:	mov    %r13,0x150(%rsp)
  271a0a:	mov    -0x60(%r14),%rsi
  271a0e:	lea    0x90(%rsp),%rdi
  271a16:	mov    0x148(%rsp),%rdx
  271a1e:	mov    $0x1,%ecx
  271a23:	call   3bb400 <tokio::runtime::io::registration::Registration::poll_ready>
  271a28:	movzbl 0x99(%rsp),%eax
  271a30:	cmp    $0x2,%eax
  271a33:	je     271bcd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x63d>
  271a39:	cmp    $0x3,%eax
  271a3c:	je     2721b5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc25>
  271a42:	xor    %eax,%eax
  271a44:	mov    %eax,%ecx
  271a46:	and    $0x3,%ecx
  271a49:	cmp    $0x1,%ecx
  271a4c:	je     271bdf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x64f>
  271a52:	jmp    271c1a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x68a>
  271a57:	nopw   0x0(%rax,%rax,1)
  271a60:	add    %r13,-0x18(%r14)
  271a64:	sub    %r13,-0x20(%r14)
  271a68:	mov    0x68(%rsp),%rax
  271a6d:	cmp    0x4a0(%rbp),%rax
  271a74:	jae    271ae0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x550>
  271a76:	mov    0x498(%rbp),%rax
  271a7d:	mov    0x60(%rsp),%rcx
  271a82:	mov    0x50(%rsp),%rdx
  271a87:	cmp    %ecx,0xb0(%rax,%rdx,1)
  271a8e:	jne    271ae0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x550>
  271a90:	add    0x50(%rsp),%rax
  271a95:	mov    0xa8(%rax),%rdi
  271a9c:	test   %rdi,%rdi
  271a9f:	je     271ae0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x550>
  271aa1:	mov    0x538(%rdi),%rax
  271aa8:	cmp    %rax,%r13
  271aab:	mov    %rax,%rsi
  271aae:	cmovb  %r13,%rsi
  271ab2:	sub    %rsi,%rax
  271ab5:	mov    %rax,0x538(%rdi)
  271abc:	mov    0x88(%rsp),%rdx
  271ac4:	mov    0x30(%rsp),%rcx
  271ac9:	mov    0x40(%rsp),%r8
  271ace:	call   1b9a00 <zfstack::conn::Conn::after_app_read>
  271ad3:	mov    %rbp,%rdi
  271ad6:	mov    0x28(%rsp),%rsi
  271adb:	call   1cb1e0 <zfstack::shard::Shard::after_conn_change>
  271ae0:	test   %r13,%r13
  271ae3:	jne    271afc <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x56c>
  271ae5:	jmp    2716f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x166>
  271aea:	nopw   0x0(%rax,%rax,1)
  271af0:	mov    %rbp,%r13
  271af3:	test   %rbp,%rbp
  271af6:	je     2716f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x166>
  271afc:	mov    -0x28(%r14),%rdx
  271b00:	test   %rdx,%rdx
  271b03:	je     273726 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2196>
  271b09:	mov    -0x30(%r14),%rax
  271b0d:	mov    -0x40(%r14),%rsi
  271b11:	mov    -0x38(%r14),%r15
  271b15:	cmp    %rsi,%rax
  271b18:	mov    $0x0,%ecx
  271b1d:	cmovae %rsi,%rcx
  271b21:	mov    %rax,%rdi
  271b24:	sub    %rcx,%rdi
  271b27:	shl    $0x5,%rdi
  271b2b:	mov    0x10(%r15,%rdi,1),%rcx
  271b30:	mov    %r13,%rbp
  271b33:	sub    %rcx,%rbp
  271b36:	jb     271c60 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6d0>
  271b3c:	lea    0x1(%rax),%rcx
  271b40:	cmp    %rsi,%rcx
  271b43:	mov    $0x0,%ecx
  271b48:	cmovae %rsi,%rcx
  271b4c:	neg    %rcx
  271b4f:	add    %rax,%rcx
  271b52:	inc    %rcx
  271b55:	mov    %rcx,-0x30(%r14)
  271b59:	dec    %rdx
  271b5c:	mov    %rdx,-0x28(%r14)
  271b60:	shl    $0x5,%rax
  271b64:	mov    (%r15,%rax,1),%rcx
  271b68:	test   %rcx,%rcx
  271b6b:	je     271af0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x560>
  271b6d:	add    %rax,%r15
  271b70:	mov    0x18(%r15),%rdi
  271b74:	mov    0x8(%r15),%rsi
  271b78:	mov    0x10(%r15),%rdx
  271b7c:	call   *0x20(%rcx)
  271b7f:	jmp    271af0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x560>
  271b84:	data16 data16 cs nopw 0x0(%rax,%rax,1)
  271b90:	mov    %r13,%rdi
  271b93:	call   459e0 <std::io::error::Error::kind>
  271b98:	cmp    $0x23,%al
  271b9a:	jne    27206c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xadc>
  271ba0:	mov    %r13d,%eax
  271ba3:	and    $0x3,%eax
  271ba6:	cmp    $0x1,%eax
  271ba9:	jne    2716f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x166>
  271baf:	mov    %r13,%rbp
  271bb2:	dec    %rbp
  271bb5:	mov    -0x1(%r13),%r15
  271bb9:	mov    0x7(%r13),%r13
  271bbd:	mov    0x0(%r13),%rax
  271bc1:	test   %rax,%rax
  271bc4:	je     271c43 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6b3>
  271bc6:	mov    %r15,%rdi
  271bc9:	call   *%rax
  271bcb:	jmp    271c43 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6b3>
  271bcd:	mov    0x90(%rsp),%rax
  271bd5:	mov    %eax,%ecx
  271bd7:	and    $0x3,%ecx
  271bda:	cmp    $0x1,%ecx
  271bdd:	jne    271c1a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x68a>
  271bdf:	lea    -0x1(%rax),%r13
  271be3:	mov    -0x1(%rax),%rbp
  271be7:	mov    0x7(%rax),%r15
  271beb:	mov    (%r15),%rax
  271bee:	test   %rax,%rax
  271bf1:	je     271bf8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x668>
  271bf3:	mov    %rbp,%rdi
  271bf6:	call   *%rax
  271bf8:	cmpq   $0x0,0x8(%r15)
  271bfd:	mov    0x24af64(%rip),%r15        # 4bcb68 <free@GLIBC_2.2.5>
  271c04:	je     271c0c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x67c>
  271c06:	mov    %rbp,%rdi
  271c09:	call   *%r15
  271c0c:	mov    %r13,%rdi
  271c0f:	call   *%r15
  271c12:	mov    0x150(%rsp),%r13
  271c1a:	mov    %r13d,%eax
  271c1d:	and    $0x3,%eax
  271c20:	cmp    $0x1,%eax
  271c23:	jne    2716f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x166>
  271c29:	lea    -0x1(%r13),%rbp
  271c2d:	mov    -0x1(%r13),%r15
  271c31:	mov    0x7(%r13),%r13
  271c35:	mov    0x0(%r13),%rax
  271c39:	test   %rax,%rax
  271c3c:	je     271c43 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x6b3>
  271c3e:	mov    %r15,%rdi
  271c41:	call   *%rax
  271c43:	cmpq   $0x0,0x8(%r13)
  271c48:	mov    0x24af19(%rip),%r13        # 4bcb68 <free@GLIBC_2.2.5>
  271c4f:	je     2716f0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  271c55:	mov    %r15,%rdi
  271c58:	call   *%r13
  271c5b:	jmp    2716f0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x160>
  271c60:	add    %rdi,%r15
  271c63:	mov    %r13,0x1c0(%rsp)
  271c6b:	test   %r13,%r13
  271c6e:	je     271cb0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x720>
  271c70:	cmp    %rcx,%r13
  271c73:	ja     273781 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21f1>
  271c79:	mov    (%r15),%rax
  271c7c:	mov    0x8(%r15),%rdx
  271c80:	lea    0x18(%r15),%rsi
  271c84:	lea    0x90(%rsp),%rdi
  271c8c:	call   *(%rax)
  271c8e:	sub    %r13,0x10(%r15)
  271c92:	add    %r13,0x8(%r15)
  271c96:	mov    0x90(%rsp),%rax
  271c9e:	mov    0x98(%rsp),%rsi
  271ca6:	mov    0xa8(%rsp),%rdi
  271cae:	jmp    271cc0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x730>
  271cb0:	mov    0x8(%r15),%rsi
  271cb4:	lea    0x240c95(%rip),%rax        # 4b2950 <__do_global_dtors_aux_fini_array_entry+0x23dc8>
  271cbb:	xor    %edi,%edi
  271cbd:	xor    %r13d,%r13d
  271cc0:	mov    %r13,%rdx
  271cc3:	call   *0x20(%rax)
  271cc6:	jmp    2716f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x166>
  271ccb:	xor    %eax,%eax
  271ccd:	jmp    273714 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2184>
  271cd2:	mov    %al,0x150(%rsp)
  271cd9:	lea    0x150(%rsp),%rax
  271ce1:	mov    %rax,0x90(%rsp)
  271ce9:	lea    0xac450(%rip),%rax        # 31e140 <<zfstack::CloseReason as core::fmt::Debug>::fmt>
  271cf0:	mov    %rax,0x98(%rsp)
  271cf8:	lea    0x160d91(%rip),%rax        # 3d2a90 <unicode_width::tables::EMOJI_MODIFIER_LEAF_4+0xe8>
  271cff:	mov    %rax,0x230(%rsp)
  271d07:	movq   $0x1,0x238(%rsp)
  271d13:	movq   $0x0,0x250(%rsp)
  271d1f:	lea    0x90(%rsp),%rax
  271d27:	mov    %rax,0x240(%rsp)
  271d2f:	movq   $0x1,0x248(%rsp)
  271d3b:	lea    0x630(%rsp),%rdi
  271d43:	lea    0x230(%rsp),%rsi
  271d4b:	call   320b10 <alloc::fmt::format::format_inner>
  271d50:	mov    0x28(%rsp),%rbx
  271d55:	lea    0x630(%rsp),%rsi
  271d5d:	mov    $0x3,%edi
  271d62:	call   457f0 <std::io::error::Error::new>
  271d67:	mov    %rax,%r12
  271d6a:	mov    (%rsp),%rax
  271d6e:	mov    %eax,%ecx
  271d70:	and    $0x3,%ecx
  271d73:	cmp    $0x1,%ecx
  271d76:	jne    271daf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x81f>
  271d78:	lea    -0x1(%rax),%r13
  271d7c:	mov    -0x1(%rax),%rbp
  271d80:	mov    0x7(%rax),%r15
  271d84:	mov    (%r15),%rax
  271d87:	test   %rax,%rax
  271d8a:	je     271d91 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x801>
  271d8c:	mov    %rbp,%rdi
  271d8f:	call   *%rax
  271d91:	cmpq   $0x0,0x8(%r15)
  271d96:	je     271da1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x811>
  271d98:	mov    %rbp,%rdi
  271d9b:	call   *0x24adc7(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  271da1:	mov    %r13,%rdi
  271da4:	call   *0x24adbe(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  271daa:	mov    0x18(%rsp),%rbp
  271daf:	mov    %r12,(%rsp)
  271db3:	mov    $0x1,%ecx
  271db8:	xor    %eax,%eax
  271dba:	mov    0x8(%rsp),%rdx
  271dbf:	lock cmpxchg %ecx,(%rdx)
  271dc3:	jne    272146 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xbb6>
  271dc9:	mov    0x24b3b8(%rip),%rax        # 4bd188 <std::panicking::panic_count::GLOBAL_PANIC_COUNT>
  271dd0:	movabs $0x8000000000000001,%rcx
  271dda:	add    $0xfffffffffffffffe,%rcx
  271dde:	mov    %rcx,0x38(%rsp)
  271de3:	test   %rcx,%rax
  271de6:	jne    272155 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xbc5>
  271dec:	movl   $0x0,0x40(%rsp)
  271df4:	mov    0x8(%rsp),%rax
  271df9:	movzbl 0x4(%rax),%eax
  271dfd:	test   %al,%al
  271dff:	jne    272171 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xbe1>
  271e05:	cmpq   $0x0,(%rsp)
  271e0a:	mov    0x8(%rsp),%rsi
  271e0f:	jne    271f71 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9e1>
  271e15:	lea    0x18(%rsi),%r12
  271e19:	nopl   0x0(%rax)
  271e20:	cmpq   $0x0,0x80(%rsi)
  271e28:	je     271f71 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9e1>
  271e2e:	cmpq   $0x0,0x48(%rsi)
  271e33:	je     271e70 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x8e0>
  271e35:	mov    0x40(%rsi),%rax
  271e39:	mov    0x30(%rsi),%rcx
  271e3d:	cmp    %rcx,%rax
  271e40:	mov    $0x0,%edx
  271e45:	cmovae %rcx,%rdx
  271e49:	mov    0x38(%rsi),%rcx
  271e4d:	sub    %rdx,%rax
  271e50:	lea    (%rax,%rax,4),%rdx
  271e54:	shl    $0x4,%rdx
  271e58:	mov    0x10(%rcx,%rdx,1),%rax
  271e5d:	mov    0x48(%rcx,%rdx,1),%rdx
  271e62:	mov    %rax,%r13
  271e65:	sub    %rdx,%r13
  271e68:	jae    271e73 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x8e3>
  271e6a:	jmp    273737 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21a7>
  271e6f:	nop
  271e70:	xor    %r13d,%r13d
  271e73:	cmp    $0x10000,%r13
  271e7a:	mov    $0x10000,%eax
  271e7f:	cmovae %rax,%r13
  271e83:	mov    0x160(%rsi),%rax
  271e8a:	mov    0x98(%rax),%rbp
  271e91:	cmpq   $0x0,0x48(%rsi)
  271e96:	je     271ef0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x960>
  271e98:	mov    0x40(%rsi),%rax
  271e9c:	mov    0x30(%rsi),%rcx
  271ea0:	cmp    %rcx,%rax
  271ea3:	mov    $0x0,%edx
  271ea8:	cmovae %rcx,%rdx
  271eac:	mov    0x38(%rsi),%rcx
  271eb0:	sub    %rdx,%rax
  271eb3:	lea    (%rax,%rax,4),%rdi
  271eb7:	shl    $0x4,%rdi
  271ebb:	mov    0x10(%rcx,%rdi,1),%rax
  271ec0:	mov    0x48(%rcx,%rdi,1),%rdx
  271ec5:	mov    %rax,%rsi
  271ec8:	sub    %rdx,%rsi
  271ecb:	jb     273737 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21a7>
  271ed1:	add    %rdi,%rcx
  271ed4:	add    0x8(%rcx),%rdx
  271ed8:	cmp    %rsi,%r13
  271edb:	jbe    271f00 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x970>
  271edd:	jmp    273751 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21c1>
  271ee2:	data16 data16 data16 data16 cs nopw 0x0(%rax,%rax,1)
  271ef0:	mov    $0x1,%edx
  271ef5:	xor    %esi,%esi
  271ef7:	cmp    %rsi,%r13
  271efa:	ja     273751 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x21c1>
  271f00:	mov    0x18(%rsp),%rdi
  271f05:	mov    %rbx,%rsi
  271f08:	mov    %r13,%rcx
  271f0b:	call   1db9d0 <zfstack::shard::Shard::write>
  271f10:	mov    %rax,%rbx
  271f13:	mov    %rdx,%r15
  271f16:	mov    0x18(%rsp),%rdi
  271f1b:	mov    0x8(%rsp),%rsi
  271f20:	mov    %rax,%rdx
  271f23:	mov    %rbp,%rcx
  271f26:	mov    %r13,%r8
  271f29:	call   1fe020 <zfstack::tokio_adapter::track_tx_wait>
  271f2e:	test   %rbx,%rbx
  271f31:	jne    271f5a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9ca>
  271f33:	mov    %r12,%rdi
  271f36:	mov    %r15,%rsi
  271f39:	call   1fdde0 <zfstack::tokio_adapter::TxQueue::consume>
  271f3e:	cmpq   $0x0,(%rsp)
  271f43:	mov    0x28(%rsp),%rbx
  271f48:	mov    0x18(%rsp),%rbp
  271f4d:	mov    0x8(%rsp),%rsi
  271f52:	je     271e20 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x890>
  271f58:	jmp    271f71 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9e1>
  271f5a:	dec    %rbx
  271f5d:	cmp    $0x3,%rbx
  271f61:	mov    0x18(%rsp),%rbp
  271f66:	mov    0x8(%rsp),%rsi
  271f6b:	jae    2720c0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb30>
  271f71:	mov    0x80(%rsi),%rbx
  271f78:	mov    0x68(%rsp),%rax
  271f7d:	cmp    0x4a0(%rbp),%rax
  271f84:	jae    27222d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc9d>
  271f8a:	mov    0x498(%rbp),%rax
  271f91:	mov    0x60(%rsp),%rcx
  271f96:	mov    0x50(%rsp),%rdx
  271f9b:	cmp    %ecx,0xb0(%rax,%rdx,1)
  271fa2:	jne    27222d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc9d>
  271fa8:	add    %rdx,%rax
  271fab:	mov    0xa8(%rax),%rax
  271fb2:	test   %rax,%rax
  271fb5:	je     27222d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc9d>
  271fbb:	xor    %edx,%edx
  271fbd:	cmpb   $0x9,0x69b(%rax)
  271fc4:	cmovne %ebx,%edx
  271fc7:	cmp    %edx,0x680(%rax)
  271fcd:	mov    0x28(%rsp),%rbx
  271fd2:	je     272217 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc87>
  271fd8:	mov    %edx,0x680(%rax)
  271fde:	mov    0x1c0(%rax),%rcx
  271fe5:	mov    %edx,%edx
  271fe7:	neg    %rdx
  271fea:	cmp    %rdx,%rcx
  271fed:	setne  %sil
  271ff1:	cmp    0x698(%rax),%sil
  271ff8:	je     272217 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc87>
  271ffe:	cmp    %rdx,%rcx
  272001:	setne  0x698(%rax)
  272008:	mov    0x450(%rax),%rsi
  27200f:	je     27220d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc7d>
  272015:	mov    0x30(%rsp),%rdi
  27201a:	call   1aa700 <zfstack::budget::Budget::sender_add>
  27201f:	jmp    272217 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc87>
  272024:	cmpb   $0x0,-0x8(%r14)
  272029:	mov    0x28(%rsp),%rbx
  27202e:	je     271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  272034:	cmpb   $0x0,-0x7(%r14)
  272039:	jne    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  27203f:	mov    -0x58(%r14),%edi
  272043:	call   3bcdf0 <tokio::net::tcp::stream::TcpStream::shutdown_std>
  272048:	mov    %rax,%r15
  27204b:	test   %rax,%rax
  27204e:	je     272203 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc73>
  272054:	cmpq   $0x0,(%rsp)
  272059:	je     272063 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xad3>
  27205b:	mov    %rsp,%rdi
  27205e:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  272063:	mov    %r15,(%rsp)
  272067:	jmp    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  27206c:	mov    (%rsp),%rax
  272070:	mov    %eax,%ecx
  272072:	and    $0x3,%ecx
  272075:	cmp    $0x1,%ecx
  272078:	mov    0x28(%rsp),%rbx
  27207d:	jne    2720b7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb27>
  27207f:	lea    -0x1(%rax),%r12
  272083:	mov    -0x1(%rax),%r15
  272087:	mov    0x7(%rax),%rbp
  27208b:	mov    0x0(%rbp),%rax
  27208f:	test   %rax,%rax
  272092:	je     272099 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb09>
  272094:	mov    %r15,%rdi
  272097:	call   *%rax
  272099:	cmpq   $0x0,0x8(%rbp)
  27209e:	je     2720a9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb19>
  2720a0:	mov    %r15,%rdi
  2720a3:	call   *0x24aabf(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2720a9:	mov    %r12,%rdi
  2720ac:	call   *0x24aab6(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2720b2:	mov    0x18(%rsp),%rbp
  2720b7:	mov    %r13,(%rsp)
  2720bb:	jmp    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  2720c0:	lea    0x168fe0(%rip),%rsi        # 3db0a7 <unicode_width::tables::EMOJI_MODIFIER_LEAF_7+0x118b>
  2720c7:	mov    $0x29,%edx
  2720cc:	mov    $0x3,%edi
  2720d1:	call   458c0 <std::io::error::Error::new>
  2720d6:	mov    %rax,%r12
  2720d9:	mov    (%rsp),%rax
  2720dd:	mov    %eax,%ecx
  2720df:	and    $0x3,%ecx
  2720e2:	cmp    $0x1,%ecx
  2720e5:	jne    27211f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb8f>
  2720e7:	lea    -0x1(%rax),%r13
  2720eb:	mov    -0x1(%rax),%r15
  2720ef:	mov    0x7(%rax),%rbp
  2720f3:	mov    0x0(%rbp),%rax
  2720f7:	test   %rax,%rax
  2720fa:	je     272101 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb71>
  2720fc:	mov    %r15,%rdi
  2720ff:	call   *%rax
  272101:	cmpq   $0x0,0x8(%rbp)
  272106:	je     272111 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xb81>
  272108:	mov    %r15,%rdi
  27210b:	call   *0x24aa57(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  272111:	mov    %r13,%rdi
  272114:	call   *0x24aa4e(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  27211a:	mov    0x18(%rsp),%rbp
  27211f:	mov    %r12,(%rsp)
  272123:	mov    0x8(%rsp),%rsi
  272128:	mov    0x80(%rsi),%rbx
  27212f:	mov    0x68(%rsp),%rax
  272134:	cmp    0x4a0(%rbp),%rax
  27213b:	jb     271f8a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x9fa>
  272141:	jmp    27222d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc9d>
  272146:	mov    0x8(%rsp),%rdi
  27214b:	call   3dd50 <std::sys::sync::mutex::futex::Mutex::lock_contended>
  272150:	jmp    271dc9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x839>
  272155:	call   3de50 <std::panicking::panic_count::is_zero_slow_path>
  27215a:	xor    $0x1,%al
  27215c:	mov    %eax,0x40(%rsp)
  272160:	mov    0x8(%rsp),%rax
  272165:	movzbl 0x4(%rax),%eax
  272169:	test   %al,%al
  27216b:	je     271e05 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x875>
  272171:	mov    0x8(%rsp),%rax
  272176:	mov    %rax,0x230(%rsp)
  27217e:	mov    0x40(%rsp),%eax
  272182:	mov    %al,0x238(%rsp)
  272189:	lea    0x1ac81c(%rip),%rdi        # 41e9ac <core::unicode::unicode_data::white_space::WHITESPACE_MAP+0x3c5f>
  272190:	lea    0x21d2a1(%rip),%rcx        # 48f438 <__do_global_dtors_aux_fini_array_entry+0x8b0>
  272197:	lea    0x222fb2(%rip),%r8        # 495150 <__do_global_dtors_aux_fini_array_entry+0x65c8>
  27219e:	lea    0x230(%rsp),%rdx
  2721a6:	mov    $0x2b,%esi
  2721ab:	call   3d4f0 <core::result::unwrap_failed>
  2721b0:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  2721b5:	mov    %r13d,%eax
  2721b8:	and    $0x3,%eax
  2721bb:	cmp    $0x1,%eax
  2721be:	mov    0x28(%rsp),%rbx
  2721c3:	jne    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  2721c9:	mov    %r13,%r12
  2721cc:	dec    %r12
  2721cf:	mov    -0x1(%r13),%r15
  2721d3:	mov    0x7(%r13),%r13
  2721d7:	mov    0x0(%r13),%rax
  2721db:	test   %rax,%rax
  2721de:	je     2721e5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc55>
  2721e0:	mov    %r15,%rdi
  2721e3:	call   *%rax
  2721e5:	cmpq   $0x0,0x8(%r13)
  2721ea:	je     2721f5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xc65>
  2721ec:	mov    %r15,%rdi
  2721ef:	call   *0x24a973(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2721f5:	mov    %r12,%rdi
  2721f8:	call   *0x24a96a(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2721fe:	jmp    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  272203:	movb   $0x1,-0x7(%r14)
  272208:	jmp    271db3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x823>
  27220d:	mov    0x30(%rsp),%rdi
  272212:	call   1aa8b0 <zfstack::budget::Budget::sender_del>
  272217:	mov    %rbp,%rdi
  27221a:	mov    %ebx,%esi
  27221c:	call   1cb1e0 <zfstack::shard::Shard::after_conn_change>
  272221:	mov    0x8(%rsp),%rax
  272226:	mov    0x80(%rax),%rbx
  27222d:	cmpb   $0x0,0x40(%rsp)
  272232:	jne    272246 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xcb6>
  272234:	mov    0x24af4d(%rip),%rax        # 4bd188 <std::panicking::panic_count::GLOBAL_PANIC_COUNT>
  27223b:	test   %rax,0x38(%rsp)
  272240:	jne    273631 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x20a1>
  272246:	xor    %eax,%eax
  272248:	mov    0x8(%rsp),%rcx
  27224d:	xchg   %eax,(%rcx)
  27224f:	cmp    $0x2,%eax
  272252:	jne    272270 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xce0>
  272254:	mov    $0xca,%edi
  272259:	mov    0x8(%rsp),%rsi
  27225e:	mov    $0x81,%edx
  272263:	mov    $0x1,%ecx
  272268:	xor    %eax,%eax
  27226a:	call   *0x24ab18(%rip)        # 4bcd88 <syscall@GLIBC_2.2.5>
  272270:	mov    (%rsp),%r12
  272274:	or     %r12,%rbx
  272277:	mov    0x28(%rsp),%rbx
  27227c:	jne    2736d8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2148>
  272282:	lea    0x558(%rbp),%rax
  272289:	mov    %rax,0x40(%rsp)
  27228e:	lea    0x220573(%rip),%rax        # 492808 <__do_global_dtors_aux_fini_array_entry+0x3c80>
  272295:	mov    %rax,0x78(%rsp)
  27229a:	lea    0x2206df(%rip),%rax        # 492980 <__do_global_dtors_aux_fini_array_entry+0x3df8>
  2722a1:	mov    %rax,0x140(%rsp)
  2722a9:	lea    0x21dda8(%rip),%rax        # 490058 <__do_global_dtors_aux_fini_array_entry+0x14d0>
  2722b0:	mov    %rax,0x138(%rsp)
  2722b8:	jmp    2722d2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd42>
  2722ba:	nopw   0x0(%rax,%rax,1)
  2722c0:	movb   $0x1,-0x6(%r14)
  2722c5:	mov    (%rsp),%r12
  2722c9:	test   %r12,%r12
  2722cc:	jne    2736dd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  2722d2:	movzbl -0x6(%r14),%eax
  2722d7:	test   %al,%al
  2722d9:	jne    2736f5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2165>
  2722df:	mov    0x8(%rsp),%rax
  2722e4:	mov    0x160(%rax),%rax
  2722eb:	mov    0x98(%rax),%r13
  2722f2:	mov    0x18(%rsp),%rax
  2722f7:	mov    0x4a0(%rax),%rax
  2722fe:	cmp    %rax,0x68(%rsp)
  272303:	jae    2724b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf20>
  272309:	mov    0x18(%rsp),%rcx
  27230e:	mov    0x498(%rcx),%rcx
  272315:	mov    0x60(%rsp),%rdx
  27231a:	mov    0x50(%rsp),%rsi
  27231f:	cmp    %edx,0xb0(%rcx,%rsi,1)
  272326:	jne    2724b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf20>
  27232c:	add    0x50(%rsp),%rcx
  272331:	mov    0xa8(%rcx),%rbp
  272338:	test   %rbp,%rbp
  27233b:	je     2724b0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf20>
  272341:	movzbl 0x69b(%rbp),%eax
  272348:	mov    $0x4,%r15d
  27234e:	cmp    $0x5,%eax
  272351:	ja     27257f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfef>
  272357:	mov    $0x26,%ecx
  27235c:	bt     %eax,%ecx
  27235f:	jae    27257f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfef>
  272365:	testb  $0x1,0x0(%rbp)
  272369:	jne    27257f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfef>
  27236f:	mov    %rbp,%rdi
  272372:	mov    0x88(%rsp),%rsi
  27237a:	mov    0x30(%rsp),%rdx
  27237f:	call   1b90b0 <zfstack::conn::Conn::send_space>
  272384:	mov    %rax,0x38(%rsp)
  272389:	mov    0x38(%rsp),%rax
  27238e:	cmp    $0x40000,%rax
  272394:	mov    $0x40000,%edx
  272399:	cmovb  %rax,%rdx
  27239d:	test   %rax,%rax
  2723a0:	je     272516 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf86>
  2723a6:	mov    0x450(%rbp),%rsi
  2723ad:	mov    0x30(%rsp),%rdi
  2723b2:	mov    %rdx,0x48(%rsp)
  2723b7:	call   1aa9e0 <zfstack::budget::Budget::try_reserve>
  2723bc:	test   %al,%al
  2723be:	je     27256d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfdd>
  2723c4:	mov    0x450(%rbp),%rsi
  2723cb:	lea    0x188(%rbp),%r15
  2723d2:	cmpb   $0x2,0x1b2(%rbp)
  2723d9:	mov    0x180(%rbp),%rax
  2723e0:	mov    0x28(%rsp),%rbx
  2723e5:	mov    $0x0,%r8d
  2723eb:	jne    272867 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12d7>
  2723f1:	cmp    $0x1,%rax
  2723f5:	jne    27243f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xeaf>
  2723f7:	mov    0x178(%rbp),%rcx
  2723fe:	mov    0x168(%rbp),%rdx
  272405:	mov    0x170(%rbp),%rax
  27240c:	cmp    %rdx,%rcx
  27240f:	cmovb  %r8,%rdx
  272413:	sub    %rdx,%rcx
  272416:	lea    (%rcx,%rcx,8),%rcx
  27241a:	cmpq   $0x10000,0x8(%rax,%rcx,8)
  272423:	jne    27243f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xeaf>
  272425:	mov    0x1c0(%rbp),%rdx
  27242c:	mov    $0x10000,%edi
  272431:	sub    %rdx,%rdi
  272434:	cmp    0x48(%rsp),%rdi
  272439:	jae    272ba4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1614>
  27243f:	mov    %rsi,%rbx
  272442:	lea    0x168(%rbp),%rdi
  272449:	mov    %rdi,0x70(%rsp)
  27244e:	call   1ae760 <zfstack::buf::TxBuf::unwrap_for_growth>
  272453:	mov    0x180(%rbp),%r12
  27245a:	test   %r12,%r12
  27245d:	je     2728e8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1358>
  272463:	mov    0x178(%rbp),%rax
  27246a:	mov    0x168(%rbp),%rcx
  272471:	mov    0x170(%rbp),%rdx
  272478:	cmp    %rcx,%rax
  27247b:	mov    $0x0,%esi
  272480:	cmovae %rcx,%rsi
  272484:	sub    %rsi,%rax
  272487:	lea    (%rax,%rax,8),%rax
  27248b:	mov    0x8(%rdx,%rax,8),%rax
  272490:	mov    %r12,%rcx
  272493:	shl    $0x10,%rcx
  272497:	lea    (%rcx,%rax,1),%rdx
  27249b:	add    $0xffffffffffff0000,%rdx
  2724a2:	jmp    2728ea <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x135a>
  2724a7:	nopw   0x0(%rax,%rax,1)
  2724b0:	movq   $0x4,0x1a0(%rsp)
  2724bc:	xor    %r15d,%r15d
  2724bf:	cmp    %rax,0x68(%rsp)
  2724c4:	jae    2725d0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  2724ca:	mov    0x18(%rsp),%rax
  2724cf:	mov    0x498(%rax),%rax
  2724d6:	mov    0x60(%rsp),%rcx
  2724db:	mov    0x50(%rsp),%rdx
  2724e0:	cmp    %ecx,0xb0(%rax,%rdx,1)
  2724e7:	jne    2725d0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  2724ed:	add    0x50(%rsp),%rax
  2724f2:	cmpq   $0x0,0xa8(%rax)
  2724fa:	je     2725d0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  272500:	mov    0x18(%rsp),%rdi
  272505:	mov    %ebx,%esi
  272507:	mov    $0x1,%edx
  27250c:	call   1caa70 <zfstack::shard::Shard::sync_share_waiter>
  272511:	jmp    2725d0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1040>
  272516:	movb   $0x1,0x696(%rbp)
  27251d:	mov    0x450(%rbp),%rsi
  272524:	mov    0x30(%rsp),%rdi
  272529:	call   1aa570 <zfstack::budget::Budget::send_share>
  27252e:	mov    %rax,%rbx
  272531:	mov    %rbp,%rdi
  272534:	mov    0x88(%rsp),%rsi
  27253c:	call   1b8e50 <zfstack::conn::Conn::sndbuf_limit>
  272541:	mov    $0x1,%r15d
  272547:	cmp    %rax,%rbx
  27254a:	mov    0x28(%rsp),%rbx
  27254f:	jae    27257f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfef>
  272551:	mov    0x18(%rsp),%rax
  272556:	mov    0x768(%rax),%rax
  27255d:	lock incq 0x98(%rax)
  272565:	mov    $0x2,%r15d
  27256b:	jmp    27257f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfef>
  27256d:	movb   $0x1,0x696(%rbp)
  272574:	mov    $0x2,%r15d
  27257a:	mov    0x28(%rsp),%rbx
  27257f:	mov    %r15,0x128(%rsp)
  272587:	movl   $0x0,0x38(%rsp)
  27258f:	mov    0x18(%rsp),%r12
  272594:	mov    %r12,%rdi
  272597:	mov    %ebx,%esi
  272599:	call   1cb1e0 <zfstack::shard::Shard::after_conn_change>
  27259e:	mov    %r15,0x1a0(%rsp)
  2725a6:	mov    %rbp,0x1a8(%rsp)
  2725ae:	cmp    $0x5,%r15
  2725b2:	sete   %r15b
  2725b6:	mov    0x4a0(%r12),%rax
  2725be:	cmp    %rax,0x68(%rsp)
  2725c3:	jb     2724ca <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xf3a>
  2725c9:	nopl   0x0(%rax)
  2725d0:	movdqu 0x1a0(%rsp),%xmm0
  2725d9:	movdqa %xmm0,0xf0(%rsp)
  2725e2:	mov    0xf0(%rsp),%r15
  2725ea:	cmp    $0x5,%r15
  2725ee:	jne    272640 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10b0>
  2725f0:	mov    0x8(%rsp),%rax
  2725f5:	movzbl 0x219(%rax),%eax
  2725fc:	test   %al,%al
  2725fe:	mov    0x18(%rsp),%rbp
  272603:	je     272632 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10a2>
  272605:	xor    %eax,%eax
  272607:	mov    0x8(%rsp),%rcx
  27260c:	xchg   %al,0x219(%rcx)
  272612:	test   %al,%al
  272614:	je     272632 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10a2>
  272616:	mov    0x8(%rsp),%rax
  27261b:	mov    0x160(%rax),%rdi
  272622:	mov    0x1d0(%rax),%rsi
  272629:	add    $0x10,%rdi
  27262d:	call   1a61b0 <zfstack::budget::GlobalBudget::remove_waiter>
  272632:	mov    0xf0(%rsp),%r15
  27263a:	jmp    27265e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x10ce>
  27263c:	nopl   0x0(%rax)
  272640:	mov    $0x40000,%r8d
  272646:	mov    0x18(%rsp),%rbp
  27264b:	mov    %rbp,%rdi
  27264e:	mov    0x8(%rsp),%rsi
  272653:	mov    %r15,%rdx
  272656:	mov    %r13,%rcx
  272659:	call   1fe020 <zfstack::tokio_adapter::track_tx_wait>
  27265e:	lea    0x162aab(%rip),%rcx        # 3d5110 <unicode_width::tables::EMOJI_MODIFIER_LEAF_4+0x2768>
  272665:	movslq (%rcx,%r15,4),%rax
  272669:	add    %rcx,%rax
  27266c:	jmp    *%rax
  27266e:	mov    0xf8(%rsp),%rax
  272676:	test   %rax,%rax
  272679:	je     27273b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x11ab>
  27267f:	add    %rax,-0x10(%r14)
  272683:	jmp    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272688:	nopl   0x0(%rax,%rax,1)
  272690:	mov    0xf8(%rsp),%r12
  272698:	mov    %r12,%rdi
  27269b:	call   459e0 <std::io::error::Error::kind>
  2726a0:	cmp    $0xd,%al
  2726a2:	jne    2726f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1166>
  2726a4:	mov    %r12,0x90(%rsp)
  2726ac:	mov    -0x60(%r14),%rsi
  2726b0:	lea    0x230(%rsp),%rdi
  2726b8:	mov    0x148(%rsp),%rdx
  2726c0:	xor    %ecx,%ecx
  2726c2:	call   3bb400 <tokio::runtime::io::registration::Registration::poll_ready>
  2726c7:	movzbl 0x239(%rsp),%eax
  2726cf:	cmp    $0x2,%eax
  2726d2:	je     2727cd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x123d>
  2726d8:	cmp    $0x3,%eax
  2726db:	je     273692 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2102>
  2726e1:	xor    %eax,%eax
  2726e3:	mov    %eax,%ecx
  2726e5:	and    $0x3,%ecx
  2726e8:	cmp    $0x1,%ecx
  2726eb:	je     2727df <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x124f>
  2726f1:	jmp    27281b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x128b>
  2726f6:	mov    %r12,%rdi
  2726f9:	call   459e0 <std::io::error::Error::kind>
  2726fe:	cmp    $0x23,%al
  272700:	jne    27364c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x20bc>
  272706:	mov    %r12d,%eax
  272709:	and    $0x3,%eax
  27270c:	cmp    $0x1,%eax
  27270f:	jne    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272715:	lea    -0x1(%r12),%r13
  27271a:	mov    -0x1(%r12),%r15
  27271f:	mov    0x7(%r12),%r12
  272724:	mov    (%r12),%rax
  272728:	test   %rax,%rax
  27272b:	je     272847 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12b7>
  272731:	mov    %r15,%rdi
  272734:	call   *%rax
  272736:	jmp    272847 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12b7>
  27273b:	movb   $0x1,-0x6(%r14)
  272740:	mov    0x68(%rsp),%rax
  272745:	cmp    0x4a0(%rbp),%rax
  27274c:	jae    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272752:	mov    0x498(%rbp),%rax
  272759:	mov    0x60(%rsp),%rcx
  27275e:	mov    0x50(%rsp),%rdx
  272763:	cmp    %ecx,0xb0(%rax,%rdx,1)
  27276a:	jne    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272770:	add    0x50(%rsp),%rax
  272775:	mov    0xa8(%rax),%rax
  27277c:	test   %rax,%rax
  27277f:	je     2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272785:	testb  $0x1,(%rax)
  272788:	jne    2728d9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1349>
  27278e:	movzbl 0x69b(%rax),%ecx
  272795:	movzbl %cl,%edx
  272798:	lea    -0x1(%rdx),%esi
  27279b:	cmp    $0x2,%esi
  27279e:	jae    2728b3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1323>
  2727a4:	mov    0x1c0(%rax),%rdx
  2727ab:	add    0x490(%rax),%rdx
  2727b2:	movq   $0x1,(%rax)
  2727b9:	mov    %rdx,0x8(%rax)
  2727bd:	mov    $0x3,%dl
  2727bf:	cmp    $0x2,%cl
  2727c2:	je     2728d3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1343>
  2727c8:	jmp    2728d9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1349>
  2727cd:	mov    0x230(%rsp),%rax
  2727d5:	mov    %eax,%ecx
  2727d7:	and    $0x3,%ecx
  2727da:	cmp    $0x1,%ecx
  2727dd:	jne    27281b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x128b>
  2727df:	lea    -0x1(%rax),%r12
  2727e3:	mov    -0x1(%rax),%r15
  2727e7:	mov    0x7(%rax),%r13
  2727eb:	mov    0x0(%r13),%rax
  2727ef:	test   %rax,%rax
  2727f2:	je     2727f9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1269>
  2727f4:	mov    %r15,%rdi
  2727f7:	call   *%rax
  2727f9:	cmpq   $0x0,0x8(%r13)
  2727fe:	mov    0x24a363(%rip),%r13        # 4bcb68 <free@GLIBC_2.2.5>
  272805:	je     27280d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x127d>
  272807:	mov    %r15,%rdi
  27280a:	call   *%r13
  27280d:	mov    %r12,%rdi
  272810:	call   *%r13
  272813:	mov    0x90(%rsp),%r12
  27281b:	mov    %r12d,%eax
  27281e:	and    $0x3,%eax
  272821:	cmp    $0x1,%eax
  272824:	jne    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  27282a:	lea    -0x1(%r12),%r13
  27282f:	mov    -0x1(%r12),%r15
  272834:	mov    0x7(%r12),%r12
  272839:	mov    (%r12),%rax
  27283d:	test   %rax,%rax
  272840:	je     272847 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12b7>
  272842:	mov    %r15,%rdi
  272845:	call   *%rax
  272847:	cmpq   $0x0,0x8(%r12)
  27284d:	mov    0x24a314(%rip),%r12        # 4bcb68 <free@GLIBC_2.2.5>
  272854:	je     27285c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x12cc>
  272856:	mov    %r15,%rdi
  272859:	call   *%r12
  27285c:	mov    %r13,%rdi
  27285f:	call   *%r12
  272862:	jmp    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  272867:	mov    0x1a0(%rbp),%rcx
  27286e:	mov    0x18(%rsp),%rdx
  272873:	cmp    0x6f8(%rdx),%rcx
  27287a:	jne    272afd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x156d>
  272880:	mov    0x700(%rdx),%rcx
  272887:	test   %rax,%rax
  27288a:	sete   %r9b
  27288e:	mov    0x1a8(%rbp),%rdx
  272895:	cmp    %rcx,%rdx
  272898:	setne  %dil
  27289c:	or     %r9b,%dil
  27289f:	je     272aeb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x155b>
  2728a5:	cmp    %rcx,%rdx
  2728a8:	jne    272afd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x156d>
  2728ae:	jmp    2723f1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe61>
  2728b3:	cmp    $0x5,%edx
  2728b6:	jne    2728d9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1349>
  2728b8:	mov    0x1c0(%rax),%rcx
  2728bf:	add    0x490(%rax),%rcx
  2728c6:	movq   $0x1,(%rax)
  2728cd:	mov    %rcx,0x8(%rax)
  2728d1:	mov    $0x7,%dl
  2728d3:	mov    %dl,0x69b(%rax)
  2728d9:	mov    %rbp,%rdi
  2728dc:	mov    %ebx,%esi
  2728de:	call   1cb1e0 <zfstack::shard::Shard::after_conn_change>
  2728e3:	jmp    2722c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xd35>
  2728e8:	xor    %edx,%edx
  2728ea:	mov    0x1c0(%rbp),%rsi
  2728f1:	add    0x1b8(%rbp),%rsi
  2728f8:	mov    0x38(%rsp),%rcx
  2728fd:	cmp    $0x10000,%rcx
  272904:	mov    $0x10000,%eax
  272909:	cmovb  %rcx,%rax
  27290d:	mov    %rdx,0x58(%rsp)
  272912:	mov    %rdx,%rcx
  272915:	mov    %rsi,0x10(%rsp)
  27291a:	sub    %rsi,%rcx
  27291d:	mov    $0x0,%edx
  272922:	cmovae %rcx,%rdx
  272926:	mov    %r12,0xe0(%rsp)
  27292e:	cmp    %rax,%rdx
  272931:	jae    2729ca <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x143a>
  272937:	mov    0xe0(%rsp),%rcx
  27293f:	test   %rcx,%rcx
  272942:	mov    $0x10000,%r12d
  272948:	mov    $0x800,%eax
  27294d:	cmove  %rax,%r12
  272951:	cmpq   $0x801,0x38(%rsp)
  27295a:	mov    $0x10000,%eax
  27295f:	cmovae %rax,%r12
  272963:	cmp    $0xffffffffffffffff,%rcx
  272967:	je     2729aa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x141a>
  272969:	lea    0x1(%rcx),%rdx
  27296d:	mov    0x70(%rsp),%rax
  272972:	mov    (%rax),%rsi
  272975:	lea    0x230(%rsp),%rdi
  27297d:	mov    0x30(%rsp),%rcx
  272982:	mov    %rbx,%r8
  272985:	call   1afc50 <zfstack::buf::TxBuf::prepare_index>
  27298a:	mov    %r15,0x20(%rsp)
  27298f:	mov    0x230(%rsp),%r15
  272997:	movabs $0x8000000000000001,%rcx
  2729a1:	cmp    %rcx,%r15
  2729a4:	jne    272b2a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x159a>
  2729aa:	xor    %eax,%eax
  2729ac:	mov    0x58(%rsp),%rcx
  2729b1:	cmp    0x10(%rsp),%rcx
  2729b6:	setbe  %cl
  2729b9:	xor    $0x1,%al
  2729bb:	test   %al,%cl
  2729bd:	jne    272ad3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1543>
  2729c3:	mov    0x180(%rbp),%r12
  2729ca:	test   %r12,%r12
  2729cd:	je     273889 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x22f9>
  2729d3:	cmpb   $0x2,0x1b2(%rbp)
  2729da:	mov    0x48(%rsp),%rbx
  2729df:	je     27387a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x22ea>
  2729e5:	movzbl 0x1b0(%rbp),%eax
  2729ec:	movb   $0x0,0x1b0(%rbp)
  2729f3:	cmp    $0x1,%al
  2729f5:	jne    272a14 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1484>
  2729f7:	mov    0x190(%rbp),%rax
  2729fe:	mov    0x1a0(%rbp),%rcx
  272a05:	lock sub %rax,0x88(%rcx)
  272a0d:	mov    0x180(%rbp),%r12
  272a14:	cmp    $0x1,%r12
  272a18:	je     272a3a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14aa>
  272a1a:	test   %r12,%r12
  272a1d:	je     273889 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x22f9>
  272a23:	mov    0x178(%rbp),%rax
  272a2a:	mov    0x168(%rbp),%rcx
  272a31:	mov    0x170(%rbp),%r15
  272a38:	jmp    272a7f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14ef>
  272a3a:	mov    0x178(%rbp),%rax
  272a41:	mov    0x168(%rbp),%rcx
  272a48:	mov    0x170(%rbp),%r15
  272a4f:	cmp    %rcx,%rax
  272a52:	mov    $0x0,%edx
  272a57:	cmovae %rcx,%rdx
  272a5b:	mov    %rax,%rsi
  272a5e:	sub    %rdx,%rsi
  272a61:	lea    (%rsi,%rsi,8),%rdx
  272a65:	cmpq   $0x10000,0x8(%r15,%rdx,8)
  272a6e:	jne    272a7f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x14ef>
  272a70:	mov    0x10(%rsp),%rdx
  272a75:	movzwl %dx,%edx
  272a78:	mov    %rdx,0x10(%rsp)
  272a7d:	jmp    272aa1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1511>
  272a7f:	cmp    %rcx,%rax
  272a82:	mov    $0x0,%edx
  272a87:	cmovae %rcx,%rdx
  272a8b:	mov    %rax,%rsi
  272a8e:	sub    %rdx,%rsi
  272a91:	lea    (%rsi,%rsi,8),%rsi
  272a95:	mov    0x10(%rsp),%rdx
  272a9a:	sub    0x8(%r15,%rsi,8),%rdx
  272a9f:	jae    272aa8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1518>
  272aa1:	xor    %edi,%edi
  272aa3:	jmp    272c8f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x16ff>
  272aa8:	mov    %rdx,%rsi
  272aab:	shr    $0x10,%rsi
  272aaf:	lea    0x1(%rsi),%rdi
  272ab3:	cmp    %rsi,%r12
  272ab6:	jbe    2738b2 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2322>
  272abc:	sub    %rdi,%r12
  272abf:	jne    272c87 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x16f7>
  272ac5:	mov    %r15,%rcx
  272ac8:	mov    %r15,%rdx
  272acb:	mov    %r15,%r8
  272ace:	jmp    272ce8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1758>
  272ad3:	mov    0x70(%rsp),%rdi
  272ad8:	call   1afb50 <zfstack::buf::TxBuf::trim_empty_index>
  272add:	mov    0x450(%rbp),%rsi
  272ae4:	mov    0x28(%rsp),%rbx
  272ae9:	jmp    272afd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x156d>
  272aeb:	mov    (%r15),%rcx
  272aee:	test   %rcx,%rcx
  272af1:	je     272afd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x156d>
  272af3:	cmp    %rsi,0x10(%rcx)
  272af7:	je     2723f1 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xe61>
  272afd:	mov    0x30(%rsp),%rdi
  272b02:	mov    0x48(%rsp),%rdx
  272b07:	call   1aaca0 <zfstack::budget::Budget::cancel_reserve>
  272b0c:	movb   $0x1,0x696(%rbp)
  272b13:	movq   $0x3,0x128(%rsp)
  272b1f:	mov    $0x3,%r15d
  272b25:	jmp    272587 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xff7>
  272b2a:	lea    0x238(%rsp),%rdx
  272b32:	mov    0x40(%rdx),%rax
  272b36:	lea    0x98(%rsp),%rsi
  272b3e:	mov    %rax,0x40(%rsi)
  272b42:	movdqu (%rdx),%xmm0
  272b46:	movdqu 0x10(%rdx),%xmm1
  272b4b:	movdqu 0x20(%rdx),%xmm2
  272b50:	movdqu 0x30(%rdx),%xmm3
  272b55:	movdqu %xmm3,0x30(%rsi)
  272b5a:	movdqu %xmm2,0x20(%rsi)
  272b5f:	movdqu %xmm1,0x10(%rsi)
  272b64:	movdqu %xmm0,(%rsi)
  272b68:	mov    %r15,0x90(%rsp)
  272b70:	lea    -0x1(%rcx),%rax
  272b74:	mov    %rax,0x80(%rsp)
  272b7c:	cmp    %rax,%r15
  272b7f:	jne    273103 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b73>
  272b85:	cmpb   $0x2,0x1b2(%rbp)
  272b8c:	jne    2731d4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c44>
  272b92:	pxor   %xmm0,%xmm0
  272b96:	movdqa %xmm0,0x230(%rsp)
  272b9f:	jmp    27320c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c7c>
  272ba4:	add    0x1b8(%rbp),%edx
  272baa:	movzwl %dx,%ebx
  272bad:	mov    $0x10000,%r8d
  272bb3:	mov    $0x10000,%edx
  272bb8:	sub    %rbx,%rdx
  272bbb:	mov    0x48(%rsp),%rsi
  272bc0:	cmp    %rdx,%rsi
  272bc3:	mov    %rdx,%r12
  272bc6:	cmovb  %rsi,%r12
  272bca:	lea    (%r12,%rbx,1),%r15
  272bce:	cmp    %rsi,%rdx
  272bd1:	cmovb  %r8,%r15
  272bd5:	lea    (%rax,%rcx,8),%rsi
  272bd9:	mov    0x10(%rax,%rcx,8),%rdi
  272bde:	mov    %r15,%rdx
  272be1:	sub    %rdi,%rdx
  272be4:	jbe    272c10 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1680>
  272be6:	cmp    $0x10000,%r15
  272bed:	ja     273922 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2392>
  272bf3:	add    (%rsi),%rdi
  272bf6:	mov    %rsi,0x10(%rsp)
  272bfb:	xor    %esi,%esi
  272bfd:	call   *0x24a0e5(%rip)        # 4bcce8 <memset@GLIBC_2.2.5>
  272c03:	mov    0x10(%rsp),%rsi
  272c08:	mov    %r15,0x10(%rsi)
  272c0c:	mov    0x8(%rsi),%r8
  272c10:	cmp    %r8,%r15
  272c13:	ja     2738d8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2348>
  272c19:	sub    %rbx,%r15
  272c1c:	cmp    %r15,%r12
  272c1f:	ja     2738e9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2359>
  272c25:	mov    0x48(%rsp),%rax
  272c2a:	sub    %r12,%rax
  272c2d:	cmp    %rbx,%rax
  272c30:	ja     2738f7 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2367>
  272c36:	mov    (%rsi),%rcx
  272c39:	add    %rcx,%rbx
  272c3c:	mov    %rbx,0x230(%rsp)
  272c44:	mov    %r12,0x238(%rsp)
  272c4c:	mov    %rcx,0x240(%rsp)
  272c54:	mov    %rax,0x248(%rsp)
  272c5c:	mov    0xe8(%rsp),%rdi
  272c64:	lea    0x230(%rsp),%rsi
  272c6c:	call   3bc820 <tokio::net::tcp::stream::TcpStream::try_read_vectored>
  272c71:	mov    %rdx,%r15
  272c74:	test   %rax,%rax
  272c77:	je     273114 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b84>
  272c7d:	mov    0x28(%rsp),%rbx
  272c82:	jmp    2730c5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b35>
  272c87:	movzwl %dx,%edx
  272c8a:	mov    %rdx,0x10(%rsp)
  272c8f:	add    %rdi,%rax
  272c92:	cmp    %rcx,%rax
  272c95:	mov    %rcx,%rdx
  272c98:	mov    $0x0,%r9d
  272c9e:	cmovb  %r9,%rdx
  272ca2:	sub    %rdx,%rax
  272ca5:	mov    %rcx,%rsi
  272ca8:	sub    %rax,%rsi
  272cab:	lea    (%rax,%r12,1),%rdi
  272caf:	cmp    %r12,%rsi
  272cb2:	cmovb  %rcx,%rdi
  272cb6:	mov    %r12,%r8
  272cb9:	sub    %rsi,%r8
  272cbc:	cmovb  %r9,%r8
  272cc0:	lea    (%rax,%rax,8),%rcx
  272cc4:	lea    (%r15,%rcx,8),%rcx
  272cc8:	lea    (%rdi,%rdi,8),%rdx
  272ccc:	lea    (%r15,%rdx,8),%rdx
  272cd0:	lea    (%r8,%r8,8),%r8
  272cd4:	lea    (%r15,%r8,8),%r8
  272cd8:	cmp    %rdi,%rax
  272cdb:	jne    272cfa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x176a>
  272cdd:	mov    %rcx,%r10
  272ce0:	mov    %rdx,%r11
  272ce3:	cmp    %r12,%rsi
  272ce6:	jb     272d06 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1776>
  272ce8:	mov    $0x1,%esi
  272ced:	mov    %rdx,%r11
  272cf0:	mov    %rcx,%r10
  272cf3:	xor    %ecx,%ecx
  272cf5:	jmp    272db8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1828>
  272cfa:	mov    %r15,%r10
  272cfd:	mov    %r8,%r11
  272d00:	mov    %rdx,%r8
  272d03:	mov    %rcx,%r15
  272d06:	mov    0x8(%r15),%r9
  272d0a:	mov    %r9,%rcx
  272d0d:	mov    0x10(%rsp),%rax
  272d12:	sub    %rax,%rcx
  272d15:	cmp    %rbx,%rcx
  272d18:	cmovae %rbx,%rcx
  272d1c:	mov    0x10(%r15),%rdi
  272d20:	lea    (%rcx,%rax,1),%r12
  272d24:	mov    %r12,%rdx
  272d27:	sub    %rdi,%rdx
  272d2a:	jbe    272d98 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1808>
  272d2c:	mov    %r11,0x1b0(%rsp)
  272d34:	mov    %r10,0x1b8(%rsp)
  272d3c:	mov    %r8,0x80(%rsp)
  272d44:	mov    %r9,0x20(%rsp)
  272d49:	cmp    %r9,%r12
  272d4c:	ja     27390b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x237b>
  272d52:	mov    %rcx,%rbx
  272d55:	mov    (%r15),%rax
  272d58:	mov    %rax,0x58(%rsp)
  272d5d:	add    %rax,%rdi
  272d60:	xor    %esi,%esi
  272d62:	call   *0x249f80(%rip)        # 4bcce8 <memset@GLIBC_2.2.5>
  272d68:	mov    0x58(%rsp),%rsi
  272d6d:	mov    %r12,0x10(%r15)
  272d71:	mov    %rbx,%rcx
  272d74:	mov    0x48(%rsp),%rbx
  272d79:	mov    0x80(%rsp),%r8
  272d81:	mov    0x20(%rsp),%r9
  272d86:	mov    0x1b8(%rsp),%r10
  272d8e:	mov    0x1b0(%rsp),%r11
  272d96:	jmp    272d9b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x180b>
  272d98:	mov    (%r15),%rsi
  272d9b:	cmp    0x10(%rsp),%r12
  272da0:	jb     27389e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x230e>
  272da6:	cmp    %r9,%r12
  272da9:	ja     2738ca <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x233a>
  272daf:	add    $0x48,%r15
  272db3:	add    0x10(%rsp),%rsi
  272db8:	cmp    %r8,%r15
  272dbb:	jne    272dc5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1835>
  272dbd:	mov    %r10,%r15
  272dc0:	cmp    %r11,%r10
  272dc3:	je     272e07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1877>
  272dc5:	mov    0x8(%r15),%rax
  272dc9:	sub    %rcx,%rbx
  272dcc:	cmp    %rbx,%rax
  272dcf:	cmovb  %rax,%rbx
  272dd3:	mov    (%r15),%r12
  272dd6:	mov    0x10(%r15),%rdi
  272dda:	mov    %rbx,%rdx
  272ddd:	sub    %rdi,%rdx
  272de0:	jbe    272e0f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x187f>
  272de2:	add    %r12,%rdi
  272de5:	mov    %rsi,0x58(%rsp)
  272dea:	xor    %esi,%esi
  272dec:	mov    %rcx,0x10(%rsp)
  272df1:	call   *0x249ef1(%rip)        # 4bcce8 <memset@GLIBC_2.2.5>
  272df7:	mov    0x58(%rsp),%rsi
  272dfc:	mov    0x10(%rsp),%rcx
  272e01:	mov    %rbx,0x10(%r15)
  272e05:	jmp    272e0f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x187f>
  272e07:	mov    $0x1,%r12d
  272e0d:	xor    %ebx,%ebx
  272e0f:	mov    %rsi,0x230(%rsp)
  272e17:	mov    %rcx,0x238(%rsp)
  272e1f:	mov    %r12,0x240(%rsp)
  272e27:	mov    %rbx,0x248(%rsp)
  272e2f:	mov    0xe8(%rsp),%rdi
  272e37:	lea    0x230(%rsp),%rsi
  272e3f:	mov    %rcx,%r12
  272e42:	call   3bc820 <tokio::net::tcp::stream::TcpStream::try_read_vectored>
  272e47:	mov    %rax,0x90(%rsp)
  272e4f:	mov    %rdx,0x98(%rsp)
  272e57:	mov    %rax,0x10(%rsp)
  272e5c:	test   %rax,%rax
  272e5f:	mov    %rdx,0x58(%rsp)
  272e64:	mov    %rdx,%rcx
  272e67:	mov    $0x0,%r15d
  272e6d:	cmovne %r15,%rcx
  272e71:	add    %r12,%rbx
  272e74:	cmp    %rbx,%rcx
  272e77:	ja     27382d <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x229d>
  272e7d:	add    0x1c0(%rbp),%rcx
  272e84:	mov    %rcx,0x1c0(%rbp)
  272e8b:	mov    0x180(%rbp),%rax
  272e92:	mov    0xe0(%rsp),%r12
  272e9a:	cmp    %r12,%rax
  272e9d:	mov    0x28(%rsp),%rbx
  272ea2:	jbe    272fd5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1a45>
  272ea8:	mov    0x1c0(%rbp),%rcx
  272eaf:	mov    0x1b8(%rbp),%rdi
  272eb6:	add    %rcx,%rdi
  272eb9:	mov    0x178(%rbp),%r8
  272ec0:	mov    0x168(%rbp),%rsi
  272ec7:	mov    0x170(%rbp),%rdx
  272ece:	cmp    %rsi,%r8
  272ed1:	mov    %rsi,%r9
  272ed4:	cmovb  %r15,%r9
  272ed8:	lea    (%r8,%rax,1),%r10
  272edc:	dec    %r10
  272edf:	sub    %r9,%r8
  272ee2:	lea    (%r8,%r8,8),%r8
  272ee6:	mov    %rax,%r9
  272ee9:	shl    $0x10,%r9
  272eed:	add    0x8(%rdx,%r8,8),%r9
  272ef2:	cmp    %rsi,%r10
  272ef5:	mov    %rsi,%r8
  272ef8:	cmovb  %r15,%r8
  272efc:	sub    %r8,%r10
  272eff:	lea    (%r10,%r10,8),%r8
  272f03:	sub    0x8(%rdx,%r8,8),%r9
  272f08:	add    $0xffffffffffff0000,%r9
  272f0f:	cmp    %r9,%rdi
  272f12:	ja     272fd5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1a45>
  272f18:	dec    %rax
  272f1b:	mov    %rax,0x180(%rbp)
  272f22:	add    0x178(%rbp),%rax
  272f29:	cmp    %rsi,%rax
  272f2c:	mov    $0x0,%ecx
  272f31:	cmovae %rsi,%rcx
  272f35:	sub    %rcx,%rax
  272f38:	lea    (%rax,%rax,8),%rcx
  272f3c:	movzbl 0x42(%rdx,%rcx,8),%eax
  272f41:	cmp    $0x2,%al
  272f43:	je     27381c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x228c>
  272f49:	lea    (%rdx,%rcx,8),%rcx
  272f4d:	movzwl 0x40(%rcx),%edx
  272f51:	mov    %dx,0x270(%rsp)
  272f59:	movdqu (%rcx),%xmm0
  272f5d:	movdqu 0x10(%rcx),%xmm1
  272f62:	movdqu 0x20(%rcx),%xmm2
  272f67:	movdqu 0x30(%rcx),%xmm3
  272f6c:	movdqa %xmm3,0x260(%rsp)
  272f75:	movdqa %xmm2,0x250(%rsp)
  272f7e:	movdqa %xmm1,0x240(%rsp)
  272f87:	movdqa %xmm0,0x230(%rsp)
  272f90:	mov    %al,0x272(%rsp)
  272f97:	movzbl 0x47(%rcx),%eax
  272f9b:	lea    0x238(%rsp),%rdx
  272fa3:	mov    %al,0x3f(%rdx)
  272fa6:	mov    0x43(%rcx),%eax
  272fa9:	mov    %eax,0x3b(%rdx)
  272fac:	mov    0x40(%rsp),%rdi
  272fb1:	lea    0x230(%rsp),%rsi
  272fb9:	call   1adb40 <zfstack::buf::BlockPool::put>
  272fbe:	mov    0x180(%rbp),%rax
  272fc5:	cmp    %r12,%rax
  272fc8:	ja     272ea8 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1918>
  272fce:	mov    0x1c0(%rbp),%rcx
  272fd5:	test   %rcx,%rcx
  272fd8:	mov    0x58(%rsp),%r15
  272fdd:	jne    2730be <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b2e>
  272fe3:	jmp    272fec <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1a5c>
  272fe5:	mov    0x180(%rbp),%rax
  272fec:	test   %rax,%rax
  272fef:	je     2730a9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b19>
  272ff5:	dec    %rax
  272ff8:	mov    %rax,0x180(%rbp)
  272fff:	add    0x178(%rbp),%rax
  273006:	mov    0x168(%rbp),%rdx
  27300d:	mov    0x170(%rbp),%rcx
  273014:	cmp    %rdx,%rax
  273017:	mov    $0x0,%esi
  27301c:	cmovae %rdx,%rsi
  273020:	sub    %rsi,%rax
  273023:	lea    (%rax,%rax,8),%rdx
  273027:	movzbl 0x42(%rcx,%rdx,8),%eax
  27302c:	cmp    $0x2,%al
  27302e:	je     2730a9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b19>
  273030:	lea    (%rcx,%rdx,8),%rcx
  273034:	movzwl 0x40(%rcx),%edx
  273038:	mov    %dx,0x220(%rsp)
  273040:	movdqu (%rcx),%xmm0
  273044:	movdqu 0x10(%rcx),%xmm1
  273049:	movdqu 0x20(%rcx),%xmm2
  27304e:	movdqu 0x30(%rcx),%xmm3
  273053:	movdqa %xmm3,0x210(%rsp)
  27305c:	movdqa %xmm2,0x200(%rsp)
  273065:	movdqa %xmm1,0x1f0(%rsp)
  27306e:	movdqa %xmm0,0x1e0(%rsp)
  273077:	mov    %al,0x222(%rsp)
  27307e:	movzbl 0x47(%rcx),%eax
  273082:	lea    0x223(%rsp),%rdx
  27308a:	mov    %al,0x4(%rdx)
  27308d:	mov    0x43(%rcx),%eax
  273090:	mov    %eax,(%rdx)
  273092:	mov    0x40(%rsp),%rdi
  273097:	lea    0x1e0(%rsp),%rsi
  27309f:	call   1adb40 <zfstack::buf::BlockPool::put>
  2730a4:	jmp    272fe5 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1a55>
  2730a9:	movq   $0x0,0x1b8(%rbp)
  2730b4:	mov    0x70(%rsp),%rdi
  2730b9:	call   1afb50 <zfstack::buf::TxBuf::trim_empty_index>
  2730be:	testb  $0x1,0x10(%rsp)
  2730c3:	je     273126 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1b96>
  2730c5:	mov    0x450(%rbp),%rsi
  2730cc:	mov    0x30(%rsp),%rdi
  2730d1:	mov    0x48(%rsp),%rdx
  2730d6:	call   1aaca0 <zfstack::budget::Budget::cancel_reserve>
  2730db:	mov    %r15,%rbp
  2730de:	mov    %r15,0x130(%rsp)
  2730e6:	movq   $0x5,0x128(%rsp)
  2730f2:	mov    $0x1,%al
  2730f4:	mov    %eax,0x38(%rsp)
  2730f8:	mov    $0x5,%r15d
  2730fe:	jmp    27258f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfff>
  273103:	movq   $0x0,0x100(%rsp)
  27310f:	jmp    27322f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c9f>
  273114:	cmp    %r15,0x48(%rsp)
  273119:	jb     273931 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23a1>
  27311f:	add    %r15,0x1c0(%rbp)
  273126:	mov    0x48(%rsp),%rdx
  27312b:	sub    %r15,%rdx
  27312e:	jbe    273141 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1bb1>
  273130:	mov    0x450(%rbp),%rsi
  273137:	mov    0x30(%rsp),%rdi
  27313c:	call   1aaca0 <zfstack::budget::Budget::cancel_reserve>
  273141:	add    %r15,0x580(%rbp)
  273148:	mov    0x1c0(%rbp),%rax
  27314f:	mov    0x680(%rbp),%ecx
  273155:	neg    %rcx
  273158:	cmp    %rcx,%rax
  27315b:	setne  %dl
  27315e:	cmp    0x698(%rbp),%dl
  273164:	je     27318f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1bff>
  273166:	cmp    %rcx,%rax
  273169:	setne  0x698(%rbp)
  273170:	mov    0x450(%rbp),%rsi
  273177:	je     273185 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1bf5>
  273179:	mov    0x30(%rsp),%rdi
  27317e:	call   1aa700 <zfstack::budget::Budget::sender_add>
  273183:	jmp    27318f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1bff>
  273185:	mov    0x30(%rsp),%rdi
  27318a:	call   1aa8b0 <zfstack::budget::Budget::sender_del>
  27318f:	cmpq   $0x3ffff,0x38(%rsp)
  273198:	mov    0x28(%rsp),%rbx
  27319d:	ja     2731ad <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c1d>
  27319f:	cmp    %r15,0x48(%rsp)
  2731a4:	jne    2731ad <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1c1d>
  2731a6:	movb   $0x1,0x696(%rbp)
  2731ad:	movq   $0x0,0x128(%rsp)
  2731b9:	mov    %r15,%rbp
  2731bc:	mov    %r15,0x130(%rsp)
  2731c4:	xor    %r15d,%r15d
  2731c7:	movl   $0x0,0x38(%rsp)
  2731cf:	jmp    27258f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xfff>
  2731d4:	xor    %r8d,%r8d
  2731d7:	cmpq   $0x0,0xe0(%rsp)
  2731e0:	sete   %r8b
  2731e4:	lea    0x230(%rsp),%rdi
  2731ec:	mov    0x20(%rsp),%rsi
  2731f1:	mov    0x30(%rsp),%rdx
  2731f6:	mov    %rbx,%rcx
  2731f9:	call   1abfa0 <zfstack::budget::MemoryLease::prepare_index_peer>
  2731fe:	testb  $0x1,0x230(%rsp)
  273206:	jne    2729aa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x141a>
  27320c:	lea    0x238(%rsp),%rax
  273214:	movdqu (%rax),%xmm0
  273218:	movdqu 0x10(%rax),%xmm1
  27321d:	movdqa %xmm1,0x110(%rsp)
  273226:	movdqa %xmm0,0x100(%rsp)
  27322f:	lea    0x230(%rsp),%rdi
  273237:	mov    0x40(%rsp),%rsi
  27323c:	mov    0x30(%rsp),%rdx
  273241:	mov    %rbx,%rcx
  273244:	mov    %r12,%r8
  273247:	call   1ad690 <zfstack::buf::BlockPool::get>
  27324c:	cmpb   $0x2,0x272(%rsp)
  273254:	jne    27329b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1d0b>
  273256:	cmpq   $0x0,0x100(%rsp)
  27325f:	je     27326e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1cde>
  273261:	lea    0x100(%rsp),%rdi
  273269:	call   130bc0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  27326e:	cmp    0x80(%rsp),%r15
  273276:	je     2729aa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x141a>
  27327c:	lea    0x90(%rsp),%rdi
  273284:	call   1349b0 <core::ptr::drop_in_place<alloc::collections::vec_deque::VecDeque<zfstack::buf::Block>>>
  273289:	lea    0xb0(%rsp),%rdi
  273291:	call   12dfa0 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
  273296:	jmp    2729aa <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x141a>
  27329b:	mov    0x270(%rsp),%rax
  2732a3:	mov    %rax,0x190(%rsp)
  2732ab:	movdqu 0x230(%rsp),%xmm0
  2732b4:	movdqu 0x240(%rsp),%xmm1
  2732bd:	movdqu 0x250(%rsp),%xmm2
  2732c6:	movdqu 0x260(%rsp),%xmm3
  2732cf:	movdqa %xmm3,0x180(%rsp)
  2732d8:	movdqa %xmm2,0x170(%rsp)
  2732e1:	movdqa %xmm1,0x160(%rsp)
  2732ea:	movdqa %xmm0,0x150(%rsp)
  2732f3:	mov    0x100(%rsp),%r12
  2732fb:	test   %r12,%r12
  2732fe:	je     273371 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1de1>
  273300:	movdqa 0x100(%rsp),%xmm0
  273309:	movdqa 0x110(%rsp),%xmm1
  273312:	movdqa %xmm1,0x1d0(%rsp)
  27331b:	movdqa %xmm0,0x1c0(%rsp)
  273324:	cmpb   $0x2,0x1b2(%rbp)
  27332b:	je     27397e <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23ee>
  273331:	movdqa 0x100(%rsp),%xmm0
  27333a:	movdqa 0x110(%rsp),%xmm1
  273343:	movdqa %xmm1,0x240(%rsp)
  27334c:	movdqa %xmm0,0x230(%rsp)
  273355:	mov    0x20(%rsp),%rdi
  27335a:	lea    0x230(%rsp),%rsi
  273362:	call   1ac4a0 <zfstack::budget::MemoryLease::commit_index_peer>
  273367:	movl   $0x0,0x20(%rsp)
  27336f:	jmp    273377 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1de7>
  273371:	mov    $0x1,%al
  273373:	mov    %eax,0x20(%rsp)
  273377:	cmp    0x80(%rsp),%r15
  27337f:	jne    2734a4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1f14>
  273385:	mov    0x190(%rsp),%rax
  27338d:	mov    %rax,0x270(%rsp)
  273395:	movdqa 0x150(%rsp),%xmm0
  27339e:	movdqa 0x160(%rsp),%xmm1
  2733a7:	movdqa 0x170(%rsp),%xmm2
  2733b0:	movdqa 0x180(%rsp),%xmm3
  2733b9:	movdqa %xmm3,0x260(%rsp)
  2733c2:	movdqa %xmm2,0x250(%rsp)
  2733cb:	movdqa %xmm1,0x240(%rsp)
  2733d4:	movdqa %xmm0,0x230(%rsp)
  2733dd:	mov    0x168(%rbp),%rcx
  2733e4:	mov    0x180(%rbp),%rax
  2733eb:	cmp    %rcx,%rax
  2733ee:	jne    273408 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1e78>
  2733f0:	mov    0x70(%rsp),%rdi
  2733f5:	call   148bb0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  2733fa:	mov    0x168(%rbp),%rcx
  273401:	mov    0x180(%rbp),%rax
  273408:	mov    0x178(%rbp),%rdx
  27340f:	add    %rax,%rdx
  273412:	cmp    %rcx,%rdx
  273415:	mov    $0x0,%esi
  27341a:	cmovae %rcx,%rsi
  27341e:	sub    %rsi,%rdx
  273421:	inc    %rax
  273424:	mov    0x170(%rbp),%rcx
  27342b:	lea    (%rdx,%rdx,8),%rdx
  27342f:	movdqa 0x230(%rsp),%xmm0
  273438:	movdqa 0x240(%rsp),%xmm1
  273441:	movdqa 0x250(%rsp),%xmm2
  27344a:	movdqa 0x260(%rsp),%xmm3
  273453:	mov    0x270(%rsp),%rsi
  27345b:	mov    %rsi,0x40(%rcx,%rdx,8)
  273460:	movdqu %xmm3,0x30(%rcx,%rdx,8)
  273466:	movdqu %xmm2,0x20(%rcx,%rdx,8)
  27346c:	movdqu %xmm1,0x10(%rcx,%rdx,8)
  273472:	movdqu %xmm0,(%rcx,%rdx,8)
  273477:	mov    %rax,0x180(%rbp)
  27347e:	test   %r12,%r12
  273481:	je     27362a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x209a>
  273487:	cmpb   $0x0,0x20(%rsp)
  27348c:	je     27362a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x209a>
  273492:	lea    0x100(%rsp),%rdi
  27349a:	call   130bc0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  27349f:	jmp    27362a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x209a>
  2734a4:	mov    0x190(%rsp),%rax
  2734ac:	mov    %rax,0x270(%rsp)
  2734b4:	movdqa 0x150(%rsp),%xmm0
  2734bd:	movdqa 0x160(%rsp),%xmm1
  2734c6:	movdqa 0x170(%rsp),%xmm2
  2734cf:	movdqa 0x180(%rsp),%xmm3
  2734d8:	movdqa %xmm3,0x260(%rsp)
  2734e1:	movdqa %xmm2,0x250(%rsp)
  2734ea:	movdqa %xmm1,0x240(%rsp)
  2734f3:	movdqa %xmm0,0x230(%rsp)
  2734fc:	mov    0xa8(%rsp),%rax
  273504:	cmp    %r15,%rax
  273507:	jne    273526 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x1f96>
  273509:	lea    0x90(%rsp),%rdi
  273511:	call   148bb0 <alloc::collections::vec_deque::VecDeque<T,A>::grow>
  273516:	mov    0x90(%rsp),%r15
  27351e:	mov    0xa8(%rsp),%rax
  273526:	mov    0xa0(%rsp),%rcx
  27352e:	add    %rax,%rcx
  273531:	cmp    %r15,%rcx
  273534:	mov    $0x0,%edx
  273539:	cmovae %r15,%rdx
  27353d:	sub    %rdx,%rcx
  273540:	mov    0x98(%rsp),%rdx
  273548:	lea    (%rcx,%rcx,8),%rcx
  27354c:	movaps 0x230(%rsp),%xmm0
  273554:	movaps 0x240(%rsp),%xmm1
  27355c:	movaps 0x250(%rsp),%xmm2
  273564:	movaps 0x260(%rsp),%xmm3
  27356c:	mov    0x270(%rsp),%rsi
  273574:	mov    %rsi,0x40(%rdx,%rcx,8)
  273579:	movups %xmm3,0x30(%rdx,%rcx,8)
  27357e:	movups %xmm2,0x20(%rdx,%rcx,8)
  273583:	movups %xmm1,0x10(%rdx,%rcx,8)
  273588:	movups %xmm0,(%rdx,%rcx,8)
  27358c:	inc    %rax
  27358f:	mov    %rax,0xa8(%rsp)
  273597:	movdqu 0x90(%rsp),%xmm0
  2735a0:	movdqu 0xb0(%rsp),%xmm1
  2735a9:	movdqu 0xc0(%rsp),%xmm2
  2735b2:	movdqu 0xd0(%rsp),%xmm3
  2735bb:	movdqa %xmm3,0x270(%rsp)
  2735c4:	movdqa %xmm2,0x260(%rsp)
  2735cd:	movdqa %xmm1,0x250(%rsp)
  2735d6:	mov    0xa0(%rsp),%rax
  2735de:	mov    %rax,0x240(%rsp)
  2735e6:	mov    0xa8(%rsp),%rax
  2735ee:	mov    %rax,0x248(%rsp)
  2735f6:	movdqa %xmm0,0x230(%rsp)
  2735ff:	mov    0x70(%rsp),%rdi
  273604:	lea    0x230(%rsp),%rsi
  27360c:	call   1afe50 <zfstack::buf::TxBuf::commit_index>
  273611:	test   %r12,%r12
  273614:	je     27362a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x209a>
  273616:	cmpb   $0x0,0x20(%rsp)
  27361b:	je     27362a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x209a>
  27361d:	lea    0x100(%rsp),%rdi
  273625:	call   130bc0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  27362a:	mov    $0x1,%al
  27362c:	jmp    2729ac <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x141c>
  273631:	call   3de50 <std::panicking::panic_count::is_zero_slow_path>
  273636:	test   %al,%al
  273638:	jne    272246 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xcb6>
  27363e:	mov    0x8(%rsp),%rax
  273643:	movb   $0x1,0x4(%rax)
  273647:	jmp    272246 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0xcb6>
  27364c:	mov    (%rsp),%rax
  273650:	mov    %eax,%ecx
  273652:	and    $0x3,%ecx
  273655:	cmp    $0x1,%ecx
  273658:	jne    27368c <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x20fc>
  27365a:	lea    -0x1(%rax),%r15
  27365e:	mov    -0x1(%rax),%r13
  273662:	mov    0x7(%rax),%r14
  273666:	mov    (%r14),%rax
  273669:	test   %rax,%rax
  27366c:	je     273673 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x20e3>
  27366e:	mov    %r13,%rdi
  273671:	call   *%rax
  273673:	cmpq   $0x0,0x8(%r14)
  273678:	je     273683 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x20f3>
  27367a:	mov    %r13,%rdi
  27367d:	call   *0x2494e5(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273683:	mov    %r15,%rdi
  273686:	call   *0x2494dc(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  27368c:	mov    %r12,(%rsp)
  273690:	jmp    2736dd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x214d>
  273692:	mov    %r12d,%eax
  273695:	and    $0x3,%eax
  273698:	cmp    $0x1,%eax
  27369b:	jne    2736d4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2144>
  27369d:	lea    -0x1(%r12),%r15
  2736a2:	mov    -0x1(%r12),%r13
  2736a7:	mov    0x7(%r12),%r12
  2736ac:	mov    (%r12),%rax
  2736b0:	test   %rax,%rax
  2736b3:	je     2736ba <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x212a>
  2736b5:	mov    %r13,%rdi
  2736b8:	call   *%rax
  2736ba:	cmpq   $0x0,0x8(%r12)
  2736c0:	je     2736cb <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x213b>
  2736c2:	mov    %r13,%rdi
  2736c5:	call   *0x24949d(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2736cb:	mov    %r15,%rdi
  2736ce:	call   *0x249494(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  2736d4:	mov    (%rsp),%r12
  2736d8:	test   %r12,%r12
  2736db:	je     2736f0 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2160>
  2736dd:	mov    $0x1,%r14b
  2736e0:	mov    %rbp,%rdi
  2736e3:	mov    %rbx,%rsi
  2736e6:	mov    %r12,%rdx
  2736e9:	call   274e60 <zfstack::tokio_adapter::Driver<E,I,F>::finish_relay>
  2736ee:	jmp    273712 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2182>
  2736f0:	movzbl -0x6(%r14),%eax
  2736f5:	test   $0x1,%al
  2736f7:	je     273712 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2182>
  2736f9:	cmpb   $0x1,-0x7(%r14)
  2736fe:	jne    273712 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2182>
  273700:	xor    %r14d,%r14d
  273703:	mov    0x18(%rsp),%rdi
  273708:	mov    %rbx,%rsi
  27370b:	xor    %edx,%edx
  27370d:	call   274e60 <zfstack::tokio_adapter::Driver<E,I,F>::finish_relay>
  273712:	mov    $0x1,%al
  273714:	add    $0x648,%rsp
  27371b:	pop    %rbx
  27371c:	pop    %r12
  27371e:	pop    %r13
  273720:	pop    %r14
  273722:	pop    %r15
  273724:	pop    %rbp
  273725:	ret
  273726:	lea    0x221a0b(%rip),%rdi        # 495138 <__do_global_dtors_aux_fini_array_entry+0x65b0>
  27372d:	call   3d5e0 <core::option::unwrap_failed>
  273732:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  273737:	lea    0x22168a(%rip),%rcx        # 494dc8 <__do_global_dtors_aux_fini_array_entry+0x6240>
  27373e:	mov    %rdx,%rdi
  273741:	mov    %rax,%rsi
  273744:	mov    %rcx,%rdx
  273747:	call   3d0e0 <core::slice::index::slice_start_index_len_fail>
  27374c:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  273751:	lea    0x221a10(%rip),%rdx        # 495168 <__do_global_dtors_aux_fini_array_entry+0x65e0>
  273758:	mov    %r13,%rdi
  27375b:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  273760:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  273765:	lea    0x2219b4(%rip),%rax        # 495120 <__do_global_dtors_aux_fini_array_entry+0x6598>
  27376c:	mov    $0x40,%esi
  273771:	mov    %rdx,%rdi
  273774:	mov    %rax,%rdx
  273777:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  27377c:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  273781:	mov    %rcx,0x1e0(%rsp)
  273789:	lea    0x1c0(%rsp),%rax
  273791:	mov    %rax,0x150(%rsp)
  273799:	lea    -0x218650(%rip),%rax        # 5b150 <core::fmt::num::<impl core::fmt::Debug for u64>::fmt>
  2737a0:	mov    %rax,0x158(%rsp)
  2737a8:	lea    0x1e0(%rsp),%rcx
  2737b0:	mov    %rcx,0x160(%rsp)
  2737b8:	mov    %rax,0x168(%rsp)
  2737c0:	lea    0x23f1b1(%rip),%rax        # 4b2978 <__do_global_dtors_aux_fini_array_entry+0x23df0>
  2737c7:	mov    %rax,0x90(%rsp)
  2737cf:	movq   $0x2,0x98(%rsp)
  2737db:	movq   $0x0,0xb0(%rsp)
  2737e7:	lea    0x150(%rsp),%rax
  2737ef:	mov    %rax,0xa0(%rsp)
  2737f7:	movq   $0x2,0xa8(%rsp)
  273803:	lea    0x23f18e(%rip),%rsi        # 4b2998 <__do_global_dtors_aux_fini_array_entry+0x23e10>
  27380a:	lea    0x90(%rsp),%rdi
  273812:	call   3d1b0 <core::panicking::panic_fmt>
  273817:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  27381c:	lea    0x21f0cd(%rip),%rdi        # 4928f0 <__do_global_dtors_aux_fini_array_entry+0x3d68>
  273823:	call   3d5e0 <core::option::unwrap_failed>
  273828:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  27382d:	lea    0x21f094(%rip),%rax        # 4928c8 <__do_global_dtors_aux_fini_array_entry+0x3d40>
  273834:	mov    %rax,0x230(%rsp)
  27383c:	movq   $0x1,0x238(%rsp)
  273848:	movq   $0x8,0x240(%rsp)
  273854:	pxor   %xmm0,%xmm0
  273858:	movdqu %xmm0,0x248(%rsp)
  273861:	lea    0x21f070(%rip),%rsi        # 4928d8 <__do_global_dtors_aux_fini_array_entry+0x3d50>
  273868:	lea    0x230(%rsp),%rdi
  273870:	call   3d1b0 <core::panicking::panic_fmt>
  273875:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  27387a:	lea    0x21f02f(%rip),%rax        # 4928b0 <__do_global_dtors_aux_fini_array_entry+0x3d28>
  273881:	mov    %rax,0x140(%rsp)
  273889:	xor    %r14d,%r14d
  27388c:	mov    0x140(%rsp),%rdi
  273894:	call   3d5e0 <core::option::unwrap_failed>
  273899:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  27389e:	mov    0x10(%rsp),%rdi
  2738a3:	lea    0x21ef76(%rip),%rax        # 492820 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2738aa:	mov    %rax,0x138(%rsp)
  2738b2:	xor    %r14d,%r14d
  2738b5:	mov    %r12,%rsi
  2738b8:	mov    0x138(%rsp),%rdx
  2738c0:	call   3d570 <core::slice::index::slice_index_order_fail>
  2738c5:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  2738ca:	mov    %r9,0x20(%rsp)
  2738cf:	lea    0x21ef4a(%rip),%rax        # 492820 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2738d6:	jmp    273906 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2376>
  2738d8:	mov    %r8,0x20(%rsp)
  2738dd:	mov    %r15,%r12
  2738e0:	lea    0x21ef39(%rip),%rax        # 492820 <__do_global_dtors_aux_fini_array_entry+0x3c98>
  2738e7:	jmp    273906 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2376>
  2738e9:	mov    %r15,0x20(%rsp)
  2738ee:	lea    0x21f013(%rip),%rax        # 492908 <__do_global_dtors_aux_fini_array_entry+0x3d80>
  2738f5:	jmp    273906 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2376>
  2738f7:	mov    %rax,%r12
  2738fa:	mov    %rbx,0x20(%rsp)
  2738ff:	lea    0x21f01a(%rip),%rax        # 492920 <__do_global_dtors_aux_fini_array_entry+0x3d98>
  273906:	mov    %rax,0x78(%rsp)
  27390b:	xor    %r14d,%r14d
  27390e:	mov    %r12,%rdi
  273911:	mov    0x20(%rsp),%rsi
  273916:	mov    0x78(%rsp),%rdx
  27391b:	call   3d1d0 <core::slice::index::slice_end_index_len_fail>
  273920:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  273922:	mov    $0x10000,%eax
  273927:	mov    %rax,0x20(%rsp)
  27392c:	mov    %r15,%r12
  27392f:	jmp    27390b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x237b>
  273931:	lea    0x21ef90(%rip),%rax        # 4928c8 <__do_global_dtors_aux_fini_array_entry+0x3d40>
  273938:	mov    %rax,0x230(%rsp)
  273940:	movq   $0x1,0x238(%rsp)
  27394c:	movq   $0x8,0x240(%rsp)
  273958:	pxor   %xmm0,%xmm0
  27395c:	movdqu %xmm0,0x248(%rsp)
  273965:	xor    %r14d,%r14d
  273968:	lea    0x21efc9(%rip),%rsi        # 492938 <__do_global_dtors_aux_fini_array_entry+0x3db0>
  27396f:	lea    0x230(%rsp),%rdi
  273977:	call   3d1b0 <core::panicking::panic_fmt>
  27397c:	jmp    27398a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x23fa>
  27397e:	lea    0x21efcb(%rip),%rdi        # 492950 <__do_global_dtors_aux_fini_array_entry+0x3dc8>
  273985:	call   3d5e0 <core::option::unwrap_failed>
  27398a:	ud2
  27398c:	mov    %rax,%rbx
  27398f:	lea    0x230(%rsp),%rdi
  273997:	call   12b750 <core::ptr::drop_in_place<zfstack::buf::Block>>
  27399c:	jmp    2739b3 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2423>
  27399e:	call   3d7b9 <core::panicking::panic_in_cleanup>
  2739a3:	mov    %rax,%rbx
  2739a6:	lea    0x230(%rsp),%rdi
  2739ae:	call   12b750 <core::ptr::drop_in_place<zfstack::buf::Block>>
  2739b3:	mov    $0x1,%bpl
  2739b6:	jmp    273a42 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24b2>
  2739bb:	call   3d7b9 <core::panicking::panic_in_cleanup>
  2739c0:	mov    %rax,%rbx
  2739c3:	xor    %ebp,%ebp
  2739c5:	jmp    273a42 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24b2>
  2739c7:	mov    %rax,%rbx
  2739ca:	lea    0xb0(%rsp),%rdi
  2739d2:	call   12dfa0 <core::ptr::drop_in_place<zfstack::budget::MemoryLease>>
  2739d7:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  2739dc:	call   3d7b9 <core::panicking::panic_in_cleanup>
  2739e1:	mov    %rax,%rbx
  2739e4:	lea    0x1c0(%rsp),%rdi
  2739ec:	call   130bc0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  2739f1:	jmp    2739f6 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2466>
  2739f3:	mov    %rax,%rbx
  2739f6:	lea    0x150(%rsp),%rdi
  2739fe:	call   12b750 <core::ptr::drop_in_place<zfstack::buf::Block>>
  273a03:	jmp    273a64 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24d4>
  273a05:	jmp    273a07 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2477>
  273a07:	mov    %rax,%rbx
  273a0a:	jmp    273a64 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24d4>
  273a0c:	mov    %rax,%rbx
  273a0f:	cmpq   $0x0,0x8(%r12)
  273a15:	je     273a20 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2490>
  273a17:	mov    %r13,%rdi
  273a1a:	call   *0x249148(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273a20:	mov    %r15,%rdi
  273a23:	call   *0x24913f(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273a29:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273a2e:	mov    %rax,%rbx
  273a31:	mov    $0x1,%bpl
  273a34:	mov    0x100(%rsp),%r12
  273a3c:	mov    $0x1,%al
  273a3e:	mov    %eax,0x20(%rsp)
  273a42:	test   %r12,%r12
  273a45:	je     273a5b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24cb>
  273a47:	cmpb   $0x0,0x20(%rsp)
  273a4c:	je     273a5b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24cb>
  273a4e:	lea    0x100(%rsp),%rdi
  273a56:	call   130bc0 <core::ptr::drop_in_place<zfstack::budget::IndexPeerReservation>>
  273a5b:	test   %bpl,%bpl
  273a5e:	je     273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273a64:	lea    0x90(%rsp),%rdi
  273a6c:	call   132900 <core::ptr::drop_in_place<core::option::Option<zfstack::buf::TxIndex>>>
  273a71:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273a76:	mov    %rax,%rbx
  273a79:	cmpq   $0x0,0x8(%r14)
  273a7e:	je     273a89 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x24f9>
  273a80:	mov    %r13,%rdi
  273a83:	call   *0x2490df(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273a89:	mov    %r15,%rdi
  273a8c:	jmp    273b32 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25a2>
  273a91:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273a96:	mov    %rax,%rbx
  273a99:	cmpq   $0x0,0x8(%r13)
  273a9e:	je     273aa9 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2519>
  273aa0:	mov    %r15,%rdi
  273aa3:	call   *0x2490bf(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273aa9:	mov    %r12,%rdi
  273aac:	call   *0x2490b6(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273ab2:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273ab7:	jmp    273b88 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25f8>
  273abc:	mov    %rax,%rbx
  273abf:	cmpq   $0x0,0x8(%rbp)
  273ac4:	je     273acf <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x253f>
  273ac6:	mov    %r15,%rdi
  273ac9:	call   *0x249099(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273acf:	mov    %r13,%rdi
  273ad2:	call   *0x249090(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273ad8:	mov    %r12,(%rsp)
  273adc:	jmp    273d00 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2770>
  273ae1:	mov    %rax,%rbx
  273ae4:	cmpq   $0x0,0x8(%rbp)
  273ae9:	je     273af4 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2564>
  273aeb:	mov    %r15,%rdi
  273aee:	call   *0x249074(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273af4:	mov    %r12,%rdi
  273af7:	call   *0x24906b(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273afd:	mov    %r13,(%rsp)
  273b01:	xor    %r14d,%r14d
  273b04:	jmp    273d1f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x278f>
  273b09:	mov    %rax,%rbx
  273b0c:	mov    %r15,(%rsp)
  273b10:	jmp    273d2a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x279a>
  273b15:	jmp    273cfd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x276d>
  273b1a:	jmp    273b88 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25f8>
  273b1c:	mov    %rax,%rbx
  273b1f:	cmpq   $0x0,0x8(%r15)
  273b24:	je     273b2f <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x259f>
  273b26:	mov    %rbp,%rdi
  273b29:	call   *0x249039(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b2f:	mov    %r13,%rdi
  273b32:	call   *0x249030(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b38:	mov    %r12,(%rsp)
  273b3c:	jmp    273d2a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x279a>
  273b41:	jmp    273b43 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25b3>
  273b43:	mov    %rax,%rbx
  273b46:	cmpq   $0x0,0x8(%r12)
  273b4c:	je     273b57 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25c7>
  273b4e:	mov    %r15,%rdi
  273b51:	call   *0x249011(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b57:	mov    %r13,%rdi
  273b5a:	call   *0x249008(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b60:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273b65:	mov    %rax,%rbx
  273b68:	cmpq   $0x0,0x8(%r13)
  273b6d:	je     273b78 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25e8>
  273b6f:	mov    %r15,%rdi
  273b72:	call   *0x248ff0(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b78:	mov    %r12,%rdi
  273b7b:	call   *0x248fe7(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273b81:	jmp    273c47 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x26b7>
  273b86:	jmp    273b88 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x25f8>
  273b88:	mov    %rax,%rbx
  273b8b:	cmpq   $0x0,0x10(%rsp)
  273b91:	je     273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273b97:	lea    0x98(%rsp),%rdi
  273b9f:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273ba4:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273ba9:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273bae:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273bb3:	jmp    273bba <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x262a>
  273bb5:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273bba:	mov    %rax,%rbx
  273bbd:	cmpq   $0x0,0x8(%r13)
  273bc2:	je     273bcd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x263d>
  273bc4:	mov    %r15,%rdi
  273bc7:	call   *0x248f9b(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273bcd:	mov    %rbp,%rdi
  273bd0:	call   *0x248f92(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273bd6:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273bdb:	mov    %rax,%rbx
  273bde:	cmpq   $0x0,0x8(%r15)
  273be3:	je     273bee <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x265e>
  273be5:	mov    %rbp,%rdi
  273be8:	call   *0x248f7a(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273bee:	mov    %r13,%rdi
  273bf1:	call   *0x248f71(%rip)        # 4bcb68 <free@GLIBC_2.2.5>
  273bf7:	jmp    273c9a <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x270a>
  273bfc:	mov    %rax,%rbx
  273bff:	test   %r15b,%r15b
  273c02:	je     273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c08:	lea    0x1a8(%rsp),%rdi
  273c10:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273c15:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c1a:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273c1f:	mov    %rax,%rbx
  273c22:	cmpb   $0x0,0x38(%rsp)
  273c27:	je     273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c2d:	lea    0x130(%rsp),%rdi
  273c35:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273c3a:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c3f:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273c44:	mov    %rax,%rbx
  273c47:	lea    0x90(%rsp),%rdi
  273c4f:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273c54:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c59:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273c5e:	mov    %rax,%rbx
  273c61:	jmp    273d1b <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x278b>
  273c66:	mov    %rax,%rbx
  273c69:	lea    0x230(%rsp),%rdi
  273c71:	call   111b60 <core::ptr::drop_in_place<std::sync::poison::PoisonError<std::sync::mutex::MutexGuard<zfstack::budget::KindCounts>>>>
  273c76:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c7b:	mov    %rax,%rbx
  273c7e:	mov    %rbp,%rdi
  273c81:	mov    %r12,%rsi
  273c84:	mov    %r13,%rdx
  273c87:	call   *0x20(%r15)
  273c8b:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273c90:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273c95:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273c97:	mov    %rax,%rbx
  273c9a:	lea    0x150(%rsp),%rdi
  273ca2:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273ca7:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273ca9:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273cab:	mov    %rax,%rbx
  273cae:	mov    0x230(%rsp),%rax
  273cb6:	test   %rax,%rax
  273cb9:	je     273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273cbb:	mov    0x248(%rsp),%rdi
  273cc3:	mov    0x238(%rsp),%rsi
  273ccb:	mov    0x240(%rsp),%rdx
  273cd3:	call   *0x20(%rax)
  273cd6:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273cd8:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273cdd:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273cdf:	mov    %rax,%rbx
  273ce2:	cmpl   $0x5,0xf0(%rsp)
  273cea:	jne    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273cec:	lea    0xf8(%rsp),%rdi
  273cf4:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273cf9:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273cfb:	jmp    273cfd <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x276d>
  273cfd:	mov    %rax,%rbx
  273d00:	movzbl 0x40(%rsp),%esi
  273d05:	mov    0x8(%rsp),%rdi
  273d0a:	call   3bbe80 <core::ptr::drop_in_place<std::sync::mutex::MutexGuard<tokio::runtime::io::registration_set::Synced>>>
  273d0f:	jmp    273d18 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2788>
  273d11:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273d13:	jmp    273d15 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x2785>
  273d15:	mov    %rax,%rbx
  273d18:	xor    %r14d,%r14d
  273d1b:	mov    (%rsp),%r13
  273d1f:	test   %r13,%r13
  273d22:	sete   %al
  273d25:	or     %r14b,%al
  273d28:	jne    273d32 <zfstack::tokio_adapter::Driver<E,I,F>::relay_pump+0x27a2>
  273d2a:	mov    %rsp,%rdi
  273d2d:	call   3b4d10 <core::ptr::drop_in_place<std::io::error::Error>>
  273d32:	mov    %rbx,%rdi
  273d35:	call   34030 <_Unwind_Resume@plt>
  273d3a:	call   3d7b9 <core::panicking::panic_in_cleanup>
  273d3f:	nop

